# Live Release Validation

Live release validation is a local operator process:

1. A developer checks out the exact commit locally.
2. They run `python -m scripts.live_release_validation` against a dedicated AWS validation account.
3. The harness deploys, validates, destroys, and writes local JSON and Markdown reports.
4. The developer reviews the result and manually uploads the Markdown report as a comment attachment on the pull request.

There is deliberately no GitHub Actions workflow for this process. Ordinary CI runs only mocked/offline contracts and must never invoke the live harness.

## Table of Contents

- [When Live Validation Is Required](#when-live-validation-is-required)
- [Safety Model](#safety-model)
- [What `--actions all` Executes](#what---actions-all-executes)
- [Local Prerequisites](#local-prerequisites)
- [Run the Script Locally](#run-the-script-locally)
- [Reports and Manual Pull Request Upload](#reports-and-manual-pull-request-upload)
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

The harness creates and destroys paid AWS infrastructure. Run it only with explicit authorization and only in a dedicated, disposable validation account.

- Acquire your team's exclusive lock for the validation account. Do not run two local validations concurrently.
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
| `api` | `topology` | Run an authenticated API Job through its complete lifecycle |
| `sqs` | `topology` | Run a direct regional SQS Job through its complete lifecycle |
| `central-queue` | `topology` | Run the idempotent DynamoDB-backed queue lifecycle |
| `convergence` | `topology` | Require stable SQS, DLQ, and DynamoDB convergence |
| `destroy` | `deploy` | Remove all exactly run-owned infrastructure in dependency order |
| `final-inventory` | `destroy` | Prove target-stack absence, accepted retained resources, and exact protected-baseline preservation |

The `configured` profile accepts the number of regional Regions already in `cdk.json`. `single-region` requires exactly one regional Region. `multi-region` requires at least two. When Job actions are selected, multi-Region validation also requires `api_gateway.regional_api_enabled=true` so observations are attributable to one Region.

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

Do not close the terminal merely because a validation action fails. The runner records the failure, then continues through its same-process cleanup path and writes the final report. `SIGTERM`, `SIGHUP`, and keyboard interruption also route through controlled cleanup when the process is still able to run.

## Reports and Manual Pull Request Upload

Every initialized run writes these local files under `$REPORT_DIR`:

- `live-release-validation.md` — human-reviewable identity, action results, cleanup, final inventory, and failures;
- `live-release-validation.json` — the same evidence in structured form; and
- `checkpoint.json` — resumable destructive authority for this exact local run.

On POSIX systems, the harness creates the dedicated report/checkpoint directory with mode `0700` and every JSON, Markdown, and temporary output with mode `0600`. It never changes permissions on a pre-existing directory: an existing output directory must already be owner-only, owned by the current operator, contain only this harness's checkpoint/report files, and not contain symlinks or special files. A custom `--checkpoint` must be a direct child of `--report-dir` and must not use either fixed report filename (`live-release-validation.json` or `live-release-validation.md`); use a new empty private directory for a fresh run.

Only the Markdown or JSON report is review evidence. **Never upload `checkpoint.json`** to a pull request, artifact service, shared drive, chat, or issue. Never commit it. Keep it local with restrictive filesystem permissions until cleanup and any recovery work are complete.

After the process exits:

1. Open `live-release-validation.md` locally.
2. Verify the exact account, full commit SHA, branch, profile, action statuses, cleanup result, and final inventory.
3. Require report status `PASSED` from a complete `--actions all` run. A successful diagnostic subset exits zero with status `PARTIAL` and lists its selected action scope; it is not release-validation evidence. A missing report, identity mismatch, incomplete cleanup, or failed final inventory is a failed validation.
4. Open the pull request for the same full SHA.
5. Add a comment stating that live validation was run locally, then manually upload `live-release-validation.md` to that comment. Optionally attach `live-release-validation.json` as additional machine-readable evidence if repository policy permits it.
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

Two ECR residual classes are accepted and reported after exact identity revalidation:

- repositories created by the run; and
- new mutable tags or digests in baseline repositories.

They are retained because ECR has no conditional repository or tag deletion primitive. Deleting after a separate read would create a time-of-check/time-of-use race and could remove content another principal changed. Review and remove accepted ECR residuals manually only under the account's normal ownership and retention procedure.

If the local process is killed before cleanup finishes:

1. Do not start a fresh run to adopt or delete the remaining stacks.
2. Preserve the local checkpoint and reports; record the exact account, branch, SHA, run ID, stack ARNs, change-set ARNs, and KMS key ARNs.
3. Restore the same checkout and credentials, then use exact local resume when it is safe to do so.
4. If exact resume is impossible, inspect the validation account read-only and compare live identities with the checkpoint, report, and CloudTrail.
5. Escalate to an authorized account operator for evidence-based recovery. Never delete by project prefix or stack name alone.
6. Record manual cleanup and final-inventory evidence in the pull request comment.

A run is successful release-validation evidence only when a complete `--actions all` report has status `PASSED`, every action passed, cleanup completed, target stacks are absent, expected `PendingDeletion` keys and accepted ECR retention are explicitly reported, and the protected baseline matches exactly. A `PARTIAL` report is diagnostic evidence only.
