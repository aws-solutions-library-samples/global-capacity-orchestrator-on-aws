# Live Release Validation

Live release validation is a local operator process:

1. A developer checks out the exact commit locally.
2. They run `python -m scripts.live_release_validation` against a dedicated, disposable AWS validation account. The account is operator-supplied, not fixed: any empty, CDK-bootstrapped account you control qualifies, and `--expected-account` pins the run to it.
3. The harness deploys, validates, destroys, and writes local JSON and Markdown reports.
4. The developer reviews the result locally and posts a sanitized summary comment (run ID, exact SHA, overall status, per-action results) on the pull request. The full reports never leave the operator's machine.

There is deliberately no GitHub Actions workflow for this process. Ordinary CI runs only mocked/offline contracts and must never invoke the live harness.

This document is the operator runbook. If you are changing the harness itself —
adding a check, adding an action, or teaching it about a new owned resource
type — read [`scripts/live_release_validation/README.md`](../scripts/live_release_validation/README.md),
which covers the package layout, the layering rules, and where each kind of
change belongs.

## Table of Contents

- [When Live Validation Is Required](#when-live-validation-is-required)
- [Safety Model](#safety-model)
- [What `--actions all` Executes](#what---actions-all-executes)
- [Local Prerequisites](#local-prerequisites)
- [Run the Script Locally](#run-the-script-locally)
- [Reports and Pull Request Evidence](#reports-and-pull-request-evidence)
- [KMS Deletion Acknowledgment](#kms-deletion-acknowledgment)
- [Exact Local Resume](#exact-local-resume)
- [Cleanup, Retained Resources, and Recovery](#cleanup-retained-resources-and-recovery)

## When Live Validation Is Required

Use live validation when a change can affect deployed infrastructure or cross-service runtime behavior that mocked and offline CI cannot establish. Apply the highest-risk row to mixed pull requests; a maintainer may also require a run when impact is uncertain.

| Decision | Typical changes |
|---|---|
| **Required** | CDK topology, CloudFormation resource lifecycle, deploy/destroy orchestration, retained-resource cleanup, IAM, networking, regional routing, EKS or Kubernetes runtime wiring, or deployed service/Lambda behavior whose correctness depends on real AWS integration |
| **Usually not required** | Isolated CLI behavior that fast mocked/offline tests fully validate; CI, workflow, or test-tooling-only changes; routine dependency bumps with no deployed runtime or infrastructure effect; documentation-only or test-only changes; and refactors with no behavior change |

The exemptions are risk-based, not filename-based. A CLI change that deploys, destroys, or mutates live AWS resources still requires validation. A dependency bump that changes a deployed image, AWS SDK behavior, CDK output, or runtime integration may require it. Document the decision in the pull request using the template. If validation is required, follow this complete runbook locally; ordinary CI must never substitute for or launch it.

## Safety Model

The harness creates and destroys paid AWS infrastructure. Run it only with explicit authorization for the target account and only in a dedicated, disposable validation account. "Dedicated" describes the account's condition, not its identity: any account you control with no other workloads and no pre-existing project resources qualifies, including a personal development account, and different operators may validate in different accounts.

- If the validation account is shared, acquire your team's exclusive lock for it first. Never run two validations concurrently against the same account.
- Use a clean, committed checkout of the exact branch and SHA under review.
- Use short-lived local AWS credentials whose account ID is known in advance.
- Never target a shared development, staging, or production account.
- A fresh run refuses pre-existing project CloudFormation stacks because it cannot prove ownership.
- Exact account, SHA, branch, absolute repository path, profile, action set, protected stacks, run ID, and KMS acknowledgment become checkpoint identity.
- Destructive cleanup requires persisted creation authority and exact live identity revalidation. A matching resource name is never ownership proof.
- Keep the local process running after a validation failure so guaranteed cleanup and final inventory can finish.

The harness never bootstraps an account. Every target Region must already contain a healthy, separately managed `CDKToolkit` stack.

## What `--actions all` Executes

Actions run in registry order. Selecting an individual action automatically includes its declared dependencies.

| Action | Depends on | Contract |
|---|---|---|
| `preflight` | None | Verify the clean Git checkout, exact AWS account, topology profile, enabled Regions, bootstrap stacks, and project ownership boundary |
| `baseline` | `preflight` | Capture protected CloudFormation and ECR state |
| `deploy` | `baseline` | Deploy the checked-in GCO topology |
| `topology` | `deploy` | Verify stacks, EKS, API endpoints, queues, and DynamoDB |
| `volume-inventory` | `topology` | Record the pre-destroy volume inventory for the selected `--volume-scenario` case: every PVC's namespace/name/UID/requested size, its bound PV identity, CSI driver, and `volumeHandle`, and the normalized identity, Region, Availability Zone, size, state, attachments, and exact cluster tag of each PVC-derived EBS volume. PVCs that produced no EBS volume are recorded with an explicit reason and validation continues. Prometheus and Alertmanager PVCs are discovered from their live component labels and their observed sizes asserted separately against `cluster_observability` (defaults `50Gi` and `5Gi`). Skipped with a reason when no volume scenario is selected |
| `api` | `topology` | Run an authenticated API Job through its complete lifecycle |
| `sqs` | `topology` | Run a direct regional SQS Job through its complete lifecycle |
| `central-queue` | `topology` | Run the idempotent DynamoDB-backed queue lifecycle, bind the worker-persisted Kubernetes identity, and verify/delete that exact workload |
| `schedulers` | `topology` | Prove every enabled batch scheduler with a scheduling-gated workload: [Volcano](https://volcano.sh/) and [YuniKorn](https://yunikorn.apache.org/) probes complete only if the named scheduler binds their pods, the [Kueue](https://kueue.sigs.k8s.io/) probe only after admission through the deployed `gco-default` queue, and the Slurm probe submits a real batch job through [slurmrestd](https://slurm.schedmd.com/rest.html) and requires `COMPLETED`. Schedulers disabled in cdk.json are recorded as skipped with their configuration source unless force-enabled with `--optional-schedulers`; KEDA (proved end to end by `sqs`) and KubeRay (chart-level, its workloads are CRDs outside the manifest gateway) carry derived evidence |
| `opencost` | `topology` | Require every Region's [OpenCost](https://opencost.io/) to be healthy and returning allocation data, then generate an ad-hoc cost report and confirm its [Parquet](https://parquet.apache.org/docs/) object in the cost report bucket (passes with a note when cost monitoring is disabled in cdk.json) |
| `convergence` | `topology` | Require stable SQS, DLQ, and DynamoDB convergence |
| `destroy` | `deploy` | Remove all exactly run-owned infrastructure in dependency order |
| `final-inventory` | `destroy` | Prove target-stack absence, accepted retained resources, and exact protected-baseline preservation |

The `configured` profile accepts the number of regional Regions already in `cdk.json`. `single-region` requires exactly one regional Region. `multi-region` requires at least two. When Job actions are selected, multi-Region validation also requires `api_gateway.regional_api_enabled=true` so observations are attributable to one Region.

## EBS Volume Cleanup Scenario

The optional EBS volume scenario proves the `destroy-all` volume-cleanup
behavior end to end against live EBS volumes. It is off by default and is
selected with `--volume-scenario`:

| `--volume-scenario` | What it exercises |
|---|---|
| `disabled` (default) | No volume inventory, no volume assertions; the `volume-inventory` action is skipped with a reason |
| `retain-override` | One lifecycle exercising `gco stacks destroy-all -y --retain-volumes`; every recorded volume must survive with its exact cluster tag |
| `delete` | One lifecycle exercising `gco stacks destroy-all -y` (implicit delete, **no** `--delete-volumes` flag and **no** volume prompt); every eligible recorded volume must be absent |
| `both` | Runs the retain-override lifecycle and then the delete lifecycle as two fully isolated, sequential deployments |

`--volume-scenario both` is a scenario-driver instruction, not a checkpoint
identity. The driver launches two sequential runs with distinct run IDs and
sibling private report directories — `<run-id>-volumes-retain-override` then
`<run-id>-volumes-delete` — each with its own checkpoint, deployment identity,
and complete deploy/destroy lifecycle. Neither case can resume or mutate the
other case's checkpoint. The single-case values (`retain-override`, `delete`)
become part of resume identity, so a `--resume` must repeat the same case.

Both lifecycles resolve their destroy command through the same command-aware
policy resolver the CLI uses. The delete case supplies the exact inputs of
`gco stacks destroy-all -y` with `delete_volumes=False`, which is what proves the
implicit-delete path needs neither a `--delete-volumes` flag nor a second volume
confirmation. The retain case supplies the exact inputs of
`gco stacks destroy-all -y --retain-volumes`. Selecting the scenario and the
credential preflight are test-harness safety gates only; they never add a CLI
volume prompt or a `--delete-volumes` requirement to the `destroy-all -y` path.

Post-destroy verification is independent of the code under test: the harness
re-describes the exact checkpointed volume IDs in their Region and requires the
retain case to find every recorded volume still present with its recorded tag,
and the delete case to find every eligible (owned, `available`, detached) volume
absent while ineligible attached or non-`available` volumes remain with a safety
outcome. The pre-destroy inventory, every `ebs-volumes` callback, the independent
post-destroy observations, and any fixture cleanup are persisted before teardown
is marked complete.

Because the retain-override case deliberately keeps paid volumes, deleting those
retained fixtures afterward requires the extra `--confirm-ebs-fixture-cleanup`
authorization. Fixture cleanup runs only after retention evidence is durable,
only for exact checkpointed run-owned identities, and only through the same
just-in-time safety recheck production uses. EBS volumes participate in
`final-inventory`, so an unresolved residual fails the run rather than leaving a
recurring bill. Without `--confirm-ebs-fixture-cleanup`, the retain case keeps
its volumes and reports them as accepted residuals for manual cleanup.

```bash
# Retain-override and implicit-delete lifecycles, cleaning up retained fixtures
python -m scripts.live_release_validation \
  --repo-root "$PWD" \
  --expected-account "$EXPECTED_ACCOUNT" \
  --expected-sha "$EXPECTED_SHA" \
  --expected-branch "$EXPECTED_BRANCH" \
  --profile configured \
  --actions all \
  --volume-scenario both \
  --confirm-ebs-fixture-cleanup \
  --run-id "$RUN_ID" \
  --report-dir "$REPORT_DIR" \
  --checkpoint "$REPORT_DIR/checkpoint.json" \
  --confirm-kms-key-deletion
```

## Local Prerequisites

Use macOS or Linux with:

- Python 3.14;
- Node 24 and the exact npm version declared in `package.json`;
- Docker available to CDK asset bundling;
- the repository's pinned CDK CLI and Python CDK dependencies;
- short-lived AWS credentials for the isolated validation account; and
- healthy CDK bootstrap stacks in every Region targeted by `cdk.json`.

From the clean repository root, install the pinned toolchain:

```bash
bash .github/scripts/use-pinned-npm.sh package.json
npm ci --ignore-scripts --no-audit --no-fund
export PATH="$PWD/node_modules/.bin:$PATH"
python -m pip install ".[cdk]"
```

Select local credentials and verify their identity before authorizing a run:

```bash
export AWS_PROFILE="gco-live-validation"
aws sts get-caller-identity
```

Confirm that the returned account is the dedicated validation account, that the configured Regions are enabled, and that no project stacks from another run remain. Credential permissions should be scoped to the validation account and the current CDK templates and harness operations rather than using a general production administrator identity.

## Run the Script Locally

Choose a durable report directory outside the checkout. The harness writes its checkpoint before preflight, so placing an unignored report directory inside the repository would make the worktree dirty and correctly fail validation.

```bash
export EXPECTED_ACCOUNT="123456789012"
export EXPECTED_SHA="$(git rev-parse HEAD)"
export EXPECTED_BRANCH="$(git symbolic-ref --short HEAD)"
export RUN_ID="local-$(date -u +%Y%m%dT%H%M%SZ)-${EXPECTED_SHA:0:12}"
export REPORT_DIR="$HOME/gco-live-release-validation-reports/$RUN_ID"

python -m scripts.live_release_validation \
  --repo-root "$PWD" \
  --expected-account "$EXPECTED_ACCOUNT" \
  --expected-sha "$EXPECTED_SHA" \
  --expected-branch "$EXPECTED_BRANCH" \
  --profile configured \
  --actions all \
  --run-id "$RUN_ID" \
  --report-dir "$REPORT_DIR" \
  --checkpoint "$REPORT_DIR/checkpoint.json" \
  --confirm-kms-key-deletion
```

This command performs real AWS deployment and deletion. Reading this runbook or copying the command is not authorization to execute it.

Add `--optional-schedulers all` (or a comma list of `yunikorn`, `slurm`) when the release touches scheduler charts, the helm installer, or scheduler-adjacent manifests: the run then force-enables the off-by-default schedulers through a run-scoped CDK context override (`helm_enabled_overrides`) — never by editing `cdk.json` — so the `schedulers` action proves them too. The override becomes part of the checkpoint identity, so a `--resume` must repeat it exactly. Without the flag, off-by-default schedulers are reported as skipped with their configuration source, which is valid evidence for releases that do not touch them.

Do not close the terminal merely because a validation action fails. The runner records the failure, then continues through its same-process cleanup path and writes the final report. `SIGTERM`, `SIGHUP`, and keyboard interruption also route through controlled cleanup when the process is still able to run.

## Reports and Pull Request Evidence

Every initialized run writes these local files under `$REPORT_DIR`:

- `live-release-validation.md` — human-reviewable identity, action results, cleanup, final inventory, and failures;
- `live-release-validation.json` — the same evidence in structured form; and
- `checkpoint.json` — resumable destructive authority for this exact local run.

On POSIX systems, the harness creates the dedicated report/checkpoint directory with mode `0700` and every JSON, Markdown, and temporary output with mode `0600`. It never changes permissions on a pre-existing directory: an existing output directory must already be owner-only, owned by the current operator, contain only this harness's checkpoint/report files, and not contain symlinks or special files. A custom `--checkpoint` must be a direct child of `--report-dir` and must not use either fixed report filename (`live-release-validation.json` or `live-release-validation.md`); use a new empty private directory for a fresh run.

Both reports are account reconnaissance material, not just review evidence. One run's Markdown report names the 12-digit validation account ID hundreds of times and enumerates CloudWatch Logs, CloudFormation, IAM, and KMS ARNs (including keys inside their live seven-day deletion window), API endpoint URLs, and the account's complete resource-naming inventory. Keep both reports local, and share a full report only through a private maintainer channel when one is explicitly requested for debugging.

**Never upload `checkpoint.json`** to a pull request, artifact service, shared drive, chat, or issue. Never commit it. Keep it local with restrictive filesystem permissions until cleanup and any recovery work are complete.

After the process exits:

1. Open `live-release-validation.md` locally.
2. Verify the exact account, full commit SHA, branch, profile, action statuses, cleanup result, and final inventory.
3. Require report status `PASSED` from a complete `--actions all` run. A successful diagnostic subset exits zero with status `PARTIAL` and lists its selected action scope; it is not release-validation evidence. A missing report, identity mismatch, incomplete cleanup, or failed final inventory is a failed validation.
4. Open the pull request for the same full SHA.
5. Add a comment containing a sanitized summary only: state that live validation ran locally, then give the run ID, the full commit SHA, the overall report status, and the per-action status table (action names, pass/fail, durations). Never post the full report, and never include account IDs, ARNs, endpoint URLs, or resource names in the comment.
6. Submit the comment. There is no bot, workflow, automatic comment, or automatic upload.

The report is tied to the exact commit. If the pull request SHA changes, run the process again for the new SHA before claiming live validation.

## KMS Deletion Acknowledgment

EKS encryption keys are retained by CloudFormation. Complete teardown schedules only keys that the checkpoint recorded and whose exact ARN, run tag, and CloudFormation stack ID still match live state.

Passing `--confirm-kms-key-deletion` authorizes those keys to enter **`PendingDeletion` with a seven-day window**. This is a real destructive operation. AWS permits cancellation during that window, but after the deletion date the key material and data encrypted solely by it are unrecoverable.

If scheduling was accidental, an authorized operator must immediately identify the exact key ARN from the local report, call KMS `CancelKeyDeletion` during the pending window, and explicitly re-enable the key if it is still needed. Never cancel or alter a key based only on a friendly alias or name.

## Exact Local Resume

Resume is only for an interrupted local run whose original `checkpoint.json` remains securely available. Use the same checkout at the same absolute path and repeat the original command with `--resume`. These values must remain byte-for-byte equivalent to checkpoint identity:

- run ID and absolute repository path;
- account, full SHA, and branch;
- profile and requested action list;
- default and additional protected-stack names; and
- KMS deletion confirmation.

Do not edit the checkpoint, move it to another checkout or machine, or use it to adopt infrastructure from another invocation. A new command without `--resume` is a fresh run and intentionally refuses pre-existing project stacks.

## Cleanup, Retained Resources, and Recovery

After exact preflight identity succeeds, normal action failures and handled signals route through same-process cleanup. Workload cleanup runs before stack cleanup; unresolved Job or central-queue evidence blocks stack teardown rather than guessing that deletion is safe. Final inventory independently rechecks stack absence and protected baselines.

The central-queue action deliberately keeps two identities separate. The requested Job name, namespace, body, and idempotency key remain immutable replay identity. After a successful terminal DynamoDB record is read consistently, the harness separately binds the worker-persisted `k8s_job_name`, `k8s_job_namespace`, and `k8s_job_uid`. Every Kubernetes lookup, log read, deletion, and absence check then uses that actual identity and requires the deterministic queue-derived name, queue ID/original-name annotations, managed-by/queue-key labels, validation run/path labels, and exact UID. Cleanup performs the same reconciliation even after an interrupted or already-complete central action; it never guesses the requested name. Both checkpoint records are validated before either is mutated, and a central workload is eligible for deletion only when terminal DynamoDB reconciliation succeeded in that cleanup attempt. A terminal failed record may omit Kubernetes identity only when the regional worker atomically persisted `workload_not_created=true` after an explicit preflight rejection and while every Kubernetes identity attribute was absent; ambiguous lookup/create failures never produce that proof.

A complete pre-destroy workload cleanup is persisted with a digest of every requested and actual workload identity after converting DynamoDB values to canonical JSON primitives. Once exact target-stack absence is observed, that barrier and the absence proof allow a later resume to continue retained-resource cleanup without rereading the now-deleted DynamoDB table. Already-destroyed checkpoints must still validate the same barrier and record fresh stack-absence proof. A legacy checkpoint with no workload records may create an explicit empty barrier; any workload-bearing missing barrier, changed workload identity, or reappearing stack fails closed.

Strict live-validation deploys pass a private CDK context that gives the three explicit custom-resource provider log groups CloudFormation `Retain` semantics. Ordinary synths and deployments keep `Delete` semantics, so routine destroy/redeploy cycles do not accumulate orphaned groups. In live validation, retention prevents a provider's final delete-event invocation from recreating a same-name generation after CloudFormation deletes the original. Before teardown, the harness checkpoints and tags each exact group ARN and creation time. Only after every exact target stack is absent does the restricted cleanup role delete that same retained generation under atomic run/token tag conditions. A changed generation remains a hard failure rather than being adopted by name.

Two ECR residual classes are accepted and reported after exact identity revalidation:

- repositories created by the run; and
- new mutable tags or digests in baseline repositories.

They are retained because ECR has no conditional repository or tag deletion primitive. Deleting after a separate read would create a time-of-check/time-of-use race and could remove content another principal changed. Review and remove accepted ECR residuals manually only under the account's normal ownership and retention procedure.

One further residual class is accepted with evidence in both `baseline` and `final-inventory`: DynamoDB streams of deleted tables. Deleting a table leaves its stream readable (`DISABLED`) for roughly 24 hours, there is no delete API for it, and the Resource Groups Tagging API keeps returning its ARN, so a prior run's correctly destroyed `gco-*` table would otherwise block the next run's clean-account gate. A tagged stream ARN is stripped only after DynamoDB itself confirms the parent table absent (`DescribeTable` → `ResourceNotFoundException`); a live table keeps its stream entry as genuine residue. Every acceptance is reported as `accepted_expired_dynamodb_streams` with the table check and observed stream status.

`final-inventory` accepts one last class on the same terms: a recorded EBS volume whose authorized deletion EC2 has begun but not finished. PVC-provisioned volumes are not CloudFormation resources, so the project scanners cannot see them and the harness accounts for this run's recorded volume IDs directly. A volume EC2 reports as `deleting` is not residual — the deletion was authorized, it is in flight, and it stops incurring storage cost once EC2 releases it — so it does not fail the run. It is reported as `accepted_pending_deletion_volumes`, each record naming the exact observation that established it (`ec2:DescribeVolumes` state). Anything else fails the run: a volume that still exists in a settled state, and a volume whose absence cannot be proved by the exact not-found error, are both reported under `ebs_volume_residuals` and raise.

If the local process is killed before cleanup finishes:

1. Do not start a fresh run to adopt or delete the remaining stacks.
2. Preserve the local checkpoint and reports; record the exact account, branch, SHA, run ID, stack ARNs, change-set ARNs, and KMS key ARNs.
3. Restore the same checkout and credentials, then use exact local resume when it is safe to do so.
4. If exact resume is impossible, inspect the validation account read-only and compare live identities with the checkpoint, report, and CloudTrail.
5. Escalate to an authorized account operator for evidence-based recovery. Never delete by project prefix or stack name alone.
6. Record manual cleanup and final-inventory evidence in the pull request comment.

A run is successful release-validation evidence only when a complete `--actions all` report has status `PASSED`, every action passed, cleanup completed, target stacks are absent, expected `PendingDeletion` keys and accepted ECR retention are explicitly reported, and the protected baseline matches exactly. A `PARTIAL` report is diagnostic evidence only.
