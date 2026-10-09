# Upgrade Validation

Upgrade validation proves the path [UPGRADING.md](UPGRADING.md) describes: an
operator running the previous release runs `gco upgrade`, and ends up with a
healthy deployment of the new one that kept the state the upgrade promises to
keep. It is a local operator process, like
[live release validation](LIVE_RELEASE_VALIDATION.md):

1. A developer checks out the exact commit locally.
2. They run `gco release validate-upgrade` against a dedicated, disposable AWS validation account.
3. The harness deploys the previous release from a private clone with that release's own `gco`, runs that `gco upgrade` to the checked-out commit, verifies the result, destroys everything, and writes local JSON and Markdown reports.
4. The developer reviews the result locally and posts a sanitized summary on the pull request.

There is no GitHub Actions workflow for it; CI runs only the offline tests of
the harness. This document is the operator runbook. The developer guide is
[`scripts/upgrade_validation/README.md`](../scripts/upgrade_validation/README.md).

## Table of Contents

- [When to Run It](#when-to-run-it)
- [Safety Model](#safety-model)
- [What `--actions all` Executes](#what---actions-all-executes)
- [Local Prerequisites](#local-prerequisites)
- [Run It](#run-it)
- [Which Code Each Release Contributes](#which-code-each-release-contributes)
- [Reports and Pull Request Evidence](#reports-and-pull-request-evidence)
- [Interruption, Resume, and Recovery](#interruption-resume-and-recovery)

## When to Run It

Run it before a release whose changes can affect the upgrade from the previous
one, in addition to live release validation:

- stack names, the split between the control plane and the regional stacks, or anything a stack retains;
- how the control plane's shared state is stored (the DynamoDB tables, the buckets, the image registry);
- cdk.json keys a previous release's configuration must still deploy with;
- the teardown and deploy orchestration in `cli/stacks.py`, or the upgrade engine in `cli/upgrade.py` (see [Which Code Each Release Contributes](#which-code-each-release-contributes)).

A pull request that changes none of these does not need it. Record the decision
in the pull request like the live-validation one.

## Safety Model

Everything in the live release validation [safety model](LIVE_RELEASE_VALIDATION.md#safety-model)
applies: explicit authorization, a dedicated disposable account with no
pre-existing project stacks, a clean checkout of the exact branch and SHA, a
healthy separately managed `CDKToolkit` stack in every target Region, and exact
identity revalidation before anything is destroyed.

One rule is deliberately different. The release harness owns a stack through
the change set it prepared itself. Here `gco` creates the stacks from another
process, so this harness, and only this harness, owns them by **run-tag
adoption**:

- the base checkout's cdk.json carries `context.tags.GcoLiveValidationRun` set to the run ID, which the app applies to every stack; the harness proves the synthesized stacks carry it before deploying anything;
- a stack is adopted when its name is one of the checkout's target stacks, it carries this run's exact tag, and CloudFormation reports it created after the phase that created it began, measured against the AWS clock recorded before the phase's command started;
- a stack the upgrade recreates is accepted as a new generation only if `gco upgrade --check` named it part of the workload tier and the previous generation is deleted; that previous generation stays on record, so the EKS key and log groups it retained are cleaned up with the run's own;
- a log group the new generation derives under a name the old one used (the EKS cluster's, a named Lambda function's) moves to the new generation: its record is rebound when the group outlived the old stack, and checkpointed afresh when the upgrade's teardown deleted it, which only stable absence or a stable different generation can prove;
- a same-name stack without the tag, created before the phase, or replacing a control-plane stack is refused, and the run fails rather than guess;
- teardown re-checks the exact stack ID and tag before every delete, as it does for prepared stacks.

The image mirror (`volcano_image_mirror`, on in the shipped cdk.json) creates
ECR repositories outside CloudFormation. A release run records each repository
its mirror creates; `gco` running in another process cannot, so before the
base deploy this harness requires every repository either release's mirror
copies into to be in the baseline already. A tag the mirror adds to one
of them is a retained image delta, as in a release run. A release validation
run leaves those repositories in place; in a fresh account, seed them first
with `gco images mirror --region <region>`.

The harness never creates a tag in your repository. The candidate commit is
tagged with a synthetic release name (the base's next patch version) in a
private mirror inside the run's workspace, and the base clone upgrades from
that mirror.

## What `--actions all` Executes

Actions run in registry order. Selecting one action includes its dependencies.

| Action | Depends on | Contract |
|---|---|---|
| `preflight` | None | The live release preflight, then the base release: its tag still names the pinned commit, the checkout descends from it, its cdk.json deploys the same project and Regions, and git, node, and npm are on `PATH` with the free-space floor met on the workspace volume. On resume, first adopts what an interrupted phase left |
| `baseline` | `preflight` | Capture protected CloudFormation and ECR state and every regional Region's Transaction Search state, as the release harness does |
| `prepare` | `preflight` | Build the private workspace (a mirror sharing objects with your repository, a clone at the base tag, a venv with the base `gco` installed editable with the `cdk` extra pinned by that release's lock file, and its `npm ci`), write the run tag and the run-scoped context into the clone's cdk.json, and synthesize the base app: its stacks must be exactly this checkout's targets, all carrying the run tag, and nothing but cdk.json may change in the clone |
| `deploy` | `baseline`, `prepare` | Require every ECR repository either release's image mirror copies into to be in the baseline, run the base release's `gco stacks deploy-all`, adopt every stack it created by run tag, checkpoint the retained EKS keys and log groups, and require every target stack deployed and owned |
| `sentinels` | `deploy` | Create a job template through the base release's API, waiting for the API to answer |
| `upgrade` | `sentinels` | Tag the candidate in the private mirror, require `gco upgrade --check` to plan exactly this upgrade (the target, an editable install, a control-plane and workload split covering the targets), checkpoint the base generation's retained resources, run the base release's `gco upgrade --yes --skip-container`, and adopt the stacks it recreated |
| `topology` | `upgrade` | The release harness's topology check against the upgraded deployment: stacks, EKS, HTTPS ALB targets, APIs, queues, and DynamoDB |
| `verify-upgrade` | `topology` | The clone is at this commit with only cdk.json differing, byte for byte what the run wrote; every control-plane stack kept its stack ID; every workload stack is a new generation of the one it replaced; the upgrade reports the target checked out, the editable install refreshed, and exactly the workload tier destroyed; and the sentinel reads back unchanged with its original creation time, then is deleted |
| `destroy` | `deploy` | Remove all run-owned infrastructure in dependency order, as the release harness does |
| `final-inventory` | `destroy` | Prove target-stack absence, accepted retained resources (both generations' EKS keys pending deletion), exact protected-baseline preservation, and Transaction Search restored |

Only a complete `--actions all` run reports `PASSED`.

## Local Prerequisites

The [live release validation prerequisites](LIVE_RELEASE_VALIDATION.md#local-prerequisites),
plus:

- `git`, `node`, and `npm` on `PATH` (the base release's CDK CLI is installed from its own lock file);
- when the base release and the candidate pin different npm versions (the `packageManager` field of `package.json`), an `npm` on `PATH` that runs whichever version the current checkout pins. Each release packages its streaming Lambda only with its own exact pin, and the harness runs every phase under one `PATH`, so a single npm fails one side: the base app's synth, or its `gco upgrade` packaging the candidate. A small wrapper that reads the nearest `package.json` carrying `packageManager` and execs the matching npm (each installed with `npm install --global --prefix <dir> npm@<version>`) covers both;
- network access to PyPI and the npm registry for the base release's toolchain;
- the free-space floor on the workspace volume as well: the workspace holds a clone of the base release (about 150 MB), its venv (about 750 MB), and its `node_modules`, and the base deploy and the upgrade build container images.

## Run It

```bash
gco release validate-upgrade \
  --expected-account 123456789012 \
  --i-understand-this-deploys-and-destroys-infrastructure \
  --confirm-kms-key-deletion
```

The command derives the SHA and branch from the checkout, picks the newest
release tag in the checkout's history as the base (pass `--base-ref vX.Y.Z` to
choose another), and writes the reports to
`~/gco-upgrade-validation-reports/<run-id>` with the workspace beside it at
`<report-dir>.workspace`. A full run deploys the base topology, destroys and
recreates the regional stacks, and tears everything down, so expect it to take
several hours.

`--confirm-kms-key-deletion` authorizes scheduling this run's retained EKS keys
for their seven-day deletion window, which here means two per Region: the
upgrade retains the base generation's key when it destroys the regional stack,
and the recreated stack creates another. See
[KMS Deletion Acknowledgment](LIVE_RELEASE_VALIDATION.md#kms-deletion-acknowledgment).

## Which Code Each Release Contributes

The upgrade runs the base release's `gco`, exactly as an operator's upgrade
does: the base release's upgrade engine and stack orchestration drive the
checkout, the reinstall, and the stack cycle, while the app, charts, and
manifests the redeploy synthesizes are the checked-out commit's. So:

- a change to the app, the stacks, or what they retain is validated by this run;
- a change to `cli/upgrade.py` or `cli/stacks.py`'s orchestration is validated by the *next* release's run, when this commit is the base. Until then its offline tests are the evidence.

The version file is not evidence: the harness verifies the upgrade by commit.

## Reports and Pull Request Evidence

Every run writes `upgrade-validation.md`, `upgrade-validation.json`, and
`checkpoint.json` under the report directory, with the same permissions and
handling rules as the [release reports](LIVE_RELEASE_VALIDATION.md#reports-and-pull-request-evidence):
they name the account and its resources, so keep them local and never upload
the checkpoint. The workspace's `logs/` directory holds the full output of
every command the harness ran; each action's report entry carries the last
lines.

On the pull request, post the run ID, the full SHA, the base release, the
overall status, and the per-action table, and nothing account-specific.

## Interruption, Resume, and Recovery

Resume works as for [the release harness](LIVE_RELEASE_VALIDATION.md#exact-local-resume):
the same checkout, the same run ID and report directory, and `--resume`. The
base release, its commit, and the workspace path are part of the checkpoint
identity too.

The two long commands, the base deploy and the upgrade, run in phases. Before a
phase's command starts, the harness checkpoints the AWS time; after it, even
when it failed or timed out, the harness adopts what it left. If the harness
itself is interrupted during a phase, `--resume` adopts what the phase left and
then fails the phase instead of running it again: an interrupted upgrade is a
failed validation, and the guaranteed cleanup tears the run down. Stopping the
harness (Ctrl-C, `SIGTERM`, a closed terminal) stops the command and every CDK
process it started. Killing it outright (`kill -9`) cannot, so the command keeps
going on its own; `--resume` then refuses to continue until it has finished or
been stopped, and names its process ID.

The workspace is deleted once teardown completes. A run that stops before then
keeps it, and the report names its path; delete it yourself when the run is
torn down (it holds no AWS state).

If cleanup cannot finish, follow the release harness's
[recovery procedure](LIVE_RELEASE_VALIDATION.md#cleanup-retained-resources-and-recovery).
The checkpoint's `owned_stacks` records list every stack generation the run
created, including the ones the upgrade replaced.
