# Floci Emulated-AWS Testing

[Floci](https://github.com/floci-io/floci) is an open-source local AWS
emulator: one HTTP endpoint speaking the real AWS wire protocol, no account,
no credentials, MIT-licensed. GCO uses it as a dedicated integration layer —
production code issuing genuine SDK requests against emulated services —
sitting between the in-process mocks and a real AWS account.

## Table of Contents

- [The three testing layers](#the-three-testing-layers)
- [How it runs](#how-it-runs)
- [Real-AWS safety](#real-aws-safety)
- [AWS services exercised](#aws-services-exercised)
- [Known emulator gaps](#known-emulator-gaps)
- [Where the E2E stops, and why](#where-the-e2e-stops-and-why)
- [Updating the Floci version](#updating-the-floci-version)

## The three testing layers

This is deliberately **not** equivalent to testing against AWS. The three
layers, and what each one proves:

| Layer | What runs | What it proves | What it cannot prove |
|---|---|---|---|
| Unit mocks (moto in-process, patched clients) | Store/CLI/stack logic against fabricated responses | Code logic, branching, error handling | Nothing about real serialization, signing, server-side semantics |
| **Floci layer (this document)** | Unmodified production classes over real HTTP against emulated AWS state | Wire-level contracts: request signing/serialization, server-enforced conditional writes, pagination cursors, redrive policies, CloudFormation materialization, the harness's inventory/identity gates | Emulator fidelity limits (below); EKS data plane, IAM enforcement, real capacity/latency behavior |
| Live release validation (`gco release validate`, real account) | The complete deploy → validate → destroy lifecycle | Actual AWS behavior end to end | — |

## How it runs

**CI (primary):** `.github/workflows/floci-tests.yml` starts the pinned Floci
image as a service container and runs two jobs — `floci:integration` (the
`tests/test_floci_*.py` modules minus the E2E) and
`floci:e2e:release-validate` (the real live-validation harness through
`gco release validate --emulator-endpoint`, preflight + baseline subset).
No AWS credentials exist anywhere in the workflow.

Neither job names its modules: each discovers them by glob, so a new Floci
test runs without a workflow edit and cannot silently go uncollected. The
split is a filename convention — name a module `tests/test_floci_*_e2e.py`
to place it in `floci:e2e:release-validate` (which provides Node, npm, and
the CDK CLI); any other `tests/test_floci_*.py` lands in `floci:integration`.
The globs are complements, so their union is always the whole layer.

**Locally (optional):**

```bash
docker run --rm -p 4566:4566 -e FLOCI_STORAGE_MODE=memory \
  floci/floci:2.0.0    # finch/podman work identically
GCO_FLOCI_ENDPOINT=http://127.0.0.1:4566 pytest tests/test_floci_*.py -v
```

The E2E module additionally needs the Node CDK toolchain on `PATH`
(`npm ci`, then `export PATH="$PWD/node_modules/.bin:$PATH"`) and a clean
worktree — the harness's own preflight enforces that.

Without `GCO_FLOCI_ENDPOINT`, every Floci module skips at collection time
(the same opt-in-env pattern as the `mooncake_image` and `helm_online`
markers), so ordinary `pytest tests/` runs are unaffected.

## Real-AWS safety

Aiming this layer at real AWS must be impossible by accident, so the guards
prove emulator behavior rather than trusting configuration, and they FAIL
(never skip) on violation:

1. the endpoint must be plain `http://` on an allow-listed local hostname —
   every real AWS endpoint is HTTPS on `*.amazonaws.com`;
2. each test session fabricates a random 12-digit `AWS_ACCESS_KEY_ID` and
   requires STS to echo it back as the caller account (Floci's documented
   multi-account routing). Real AWS can never pass this probe: a fabricated
   key id fails signature validation outright.

The same two proofs gate the harness's CI exception
(`scripts/live_release_validation/emulator.py`): `require_local_execution`
still refuses GitHub Actions unless `GCO_LIVE_VALIDATION_EMULATOR` names an
endpoint that passes both. The fabricated account id also gives per-session
isolation against a shared, long-lived local emulator.

## AWS services exercised

Verified empirically against Floci 1.6.0 (every row is exercised by the
committed tests, not inferred from Floci's docs):

| Service | GCO path under test | Depth |
|---|---|---|
| DynamoDB | `TemplateStore`/`WebhookStore`/`JobStore`/`InferenceEndpointStore`; `capacity-poller` snapshot writes | Meaningful behavior: conditional writes, GSIs, pagination, waiters |
| SQS | `queue_processor` consume path, queue+DLQ redrive pair | Meaningful behavior incl. server-side redrive to the DLQ |
| S3 | `CostMonitor` Parquet reports; presigned URLs | Meaningful behavior incl. `head_object` idempotency |
| Secrets Manager | `auth_middleware` token load + rotation stages; `secret-rotation` Lambda four-step protocol | Meaningful behavior (staging labels, promotion, per-token idempotency) |
| CloudFormation | `GCOAWSClient` discovery; harness fingerprints; `cross-region-aggregator` bridge discovery; E2E CDKToolkit/`cdk list` | Stack materialization, outputs, waiters, tags, fail-closed + bounded-stale discovery (see gaps) |
| STS | preflight/emulator identity verification | Control plane (identity echo) |
| EC2 | enabled-region discovery; VPC/subnet scaffolding for ALB fixtures; `capacity-poller` degraded-signal path (spot/capacity-block APIs reject with `ClientError`) | Control plane (see AZ-id and capacity-API gaps) |
| SSM | `aws_ssm` helpers; CFN-provisioned parameters | Meaningful behavior |
| Step Functions | `helm-orchestrator` provider (start/adopt/fence), `helm-installer` teardown provider (ordered delete, drain, `is_complete`) | Meaningful behavior: named executions, `ExecutionAlreadyExists` adoption, stop confirmation, Fail-state error/cause |
| ELBv2 | `regional-api-proxy` ownership validation; `ga-registration` tag/hostname ALB discovery | Meaningful behavior: real internal ALBs, tags, fail-closed rejections |
| ECR | `image-lookup` adopt-or-create custom resource | Meaningful behavior in CI (create/adopt/delete); local Finch hosts skip (gap 4) |
| EKS / Lambda / Logs / IAM / KMS / API Gateway / Tagging API | Harness inventory scanners | Control-plane list/describe only |
| CloudWatch | `metrics_publisher`; `traffic-dial-controller` decision metrics and its no-datapoints `GetMetricData` hold | Accepts writes (no query assertions); an empty query answer drives the controller's fail-safe hold for real |

Still mocked (unit layer only): Kubernetes API interactions (kind E2E owns
live-cluster behavior), Bedrock advisors, Cost Explorer analytics, Cognito
analytics users, SageMaker/EFS/FSx cleanup paths (`analytics-cleanup`,
`analytics-presigned-url`), CloudFormation drift detection
(`drift-detection`; `DetectStackDrift` is absent from the emulator), the
Global Accelerator and EKS halves of `ga-registration`, the Global
Accelerator half of `traffic-dial-controller` (its SSM and CloudWatch halves
run here; see `test_floci_traffic_dial.py`), and every
kubeconfig-dependent Lambda path (`kubectl-applier-simple`,
`helm-installer` worker, `tls-certificate-manager`): emulator EKS clusters
never reach `ACTIVE`, so in-cluster behavior cannot be exercised honestly.

## Known emulator gaps

Each gap below was probed empirically; the first three have narrow,
documented answers in `tests/_floci_gap_shims.py` (a botocore `before-send`
handler per read-only operation — production code is untouched; harness
subprocesses receive them via `tests/_floci_sitecustomize/`):

1. **CloudFormation `GetStackPolicy`** responses omit the result wrapper and
   are unparseable by botocore. The shim answers with real AWS's no-policy
   shape — which is what every GCO stack has.
2. **Global Accelerator** is absent from Floci's catalog, while the
   harness's fail-closed inventory requires its scanner to complete. The
   shim answers `ListAccelerators` with the truthful empty list; GA scanner
   logic keeps its coverage in patched-client unit tests.
3. **Availability Zone ids** are not modeled by Floci's EC2. A credentialed
   synth runs the regional stack's fail-closed EKS-unsupported-AZ
   resolution (`DescribeAvailabilityZones` filtered by `zone-id`); the shim
   answers only that filtered query with the canonical id→name mapping so
   the fail-closed logic stays exercised instead of bypassed.
4. **ECR repository creation** is Docker-backed inside Floci and requires a
   Docker socket in the emulator container. The `floci:integration` job
   mounts the runner's socket so the `image-lookup` paths run for real in
   CI; under Finch/containerd-only local setups (no socket to mount)
   `CreateRepository` fails with `InternalFailure` and those tests skip
   themselves with that reason.
5. **EC2 capacity APIs** (`GetSpotPlacementScores`,
   `DescribeSpotPriceHistory`, `DescribeCapacityBlockOfferings`) reject with
   `ClientError`. No shim: the capacity poller's degraded-signal handling is
   the production behavior under test.
6. **Step Functions `ListExecutions`** keeps returning stopped executions
   under `statusFilter=RUNNING`. The teardown drain-loop's
   eventually-reports-zero contract therefore stays in the unit suite; the
   wire-level tests cover start/stop/adopt/describe semantics, which the
   emulator models faithfully.

## Where the E2E stops, and why

`floci:e2e:release-validate` runs the complete preflight → baseline path of
the real harness: git identity pinning, STS account verification, region
discovery, a full `cdk list` (which synthesizes the entire five-stack app),
per-region CDKToolkit health checks, refusal of pre-existing project stacks,
protected-baseline capture, report/checkpoint writing, and PARTIAL-status
semantics — plus the negative proof that an account mismatch fails the run.

The `deploy` action and everything behind it (topology, job lifecycles,
destroy of a deployed topology) is not run against the emulator. Reasons,
by category:

- **Fundamental:** the deployment's core is an EKS cluster with GPU
  nodepools, Karpenter, ALB Gateway routing, and IRSA; no emulator
  meaningfully reproduces that, and pretending otherwise would test the
  emulator, not GCO. The kind E2E (`integration:kind:cluster-e2e`) owns
  live-Kubernetes behavior; the real harness owns AWS behavior.
- **Floci limits:** CDK asset publishing needs ECR + Docker-in-Docker depth
  and CloudFormation coverage for several dozen resource types (EKS custom
  resources, WAF, Aurora, FSx) that are untested territory; each would need
  its own verification before we could trust a green result.
- **CI budget:** one full synth already costs ~5 minutes; a six-stack
  deploy attempt with container builds would multiply that for coverage of
  uncertain value.

## Updating the Floci version

1. Pick the new tag and resolve its digest:
   `docker manifest inspect floci/floci:<tag>`.
2. Update the two `image:` pins in `.github/workflows/floci-tests.yml` and
   the version references in this document.
3. Re-probe the documented gaps: run the Floci suite locally; if a shimmed
   operation now parses/exists upstream, delete the corresponding shim in
   `tests/_floci_gap_shims.py` (and this document's row). The E2E fails
   loudly if a new gap appears.
4. There is no automatic bump: Dependabot's docker ecosystem only watches
   `/dockerfiles`, so this pin moves deliberately, with the re-probe.
