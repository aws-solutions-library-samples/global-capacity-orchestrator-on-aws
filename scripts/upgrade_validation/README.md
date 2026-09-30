# Upgrade Validation Harness

This package proves `gco upgrade` works: the previous release, deployed by its
own `gco`, is upgraded to the checked-out commit by that same `gco upgrade`,
and the result is checked against what the upgrade promises.

[`docs/UPGRADE_VALIDATION.md`](../../docs/UPGRADE_VALIDATION.md) is the
operator runbook: when to run it, its safety model, and how to run it. **This**
file is the developer guide: how the code is organized and why it is shaped
the way it is.

The harness is a thin layer over
[`scripts/live_release_validation`](../live_release_validation/README.md):
`registry.py` reuses that package's baseline, topology, destroy, and
final-inventory actions verbatim, and its runner executes the registry. Read
that README first for the run model (identity gates, checkpoints, ownership,
reporting).

## Table of Contents

- [Layout](#layout)
- [How a run executes](#how-a-run-executes)
- [Why ownership works differently here](#why-ownership-works-differently-here)
- [The workspace](#the-workspace)
- [Phases and resume](#phases-and-resume)
- [Testing your change](#testing-your-change)

## Layout

| Path | Responsibility |
|---|---|
| `__main__.py` | CLI entry (`python -m scripts.upgrade_validation`): the sibling harness's identity flags, `--base-ref` (by default the newest release tag in the checkout's history that is not the checkout), and `--workspace-dir`. Removes the workspace once teardown completed. `gco release validate-upgrade` is the no-prompt wrapper around it. |
| `registry.py` | The ordered action registry: four reused actions plus the six in `actions.py`. Held in lockstep with the contract table in `docs/UPGRADE_VALIDATION.md` by `tests/test_upgrade_validation.py`. |
| `models.py` | `UpgradeRunSettings`: the sibling's `RunSettings` plus the base release (the tag, pinned to its commit) and the workspace, all part of `identity()`. It is the only settings class with `allows_run_tag_adoption`. |
| `actions.py` | `preflight`, `prepare`, `deploy`, `sentinels`, `upgrade`, and `verify-upgrade`, and the phase bookkeeping `deploy` and `upgrade` share. |
| `workspace.py` | Everything local: the private git mirror and clone, the run-scoped cdk.json, the cloud-assembly reader, the environment the base `gco` runs in, and `run_logged`, which runs a command in its own session with its output logged and mirrored to the console. |
| `sentinel.py` | The job template written through the API before the upgrade and required unchanged after it. |

## How a run executes

| Action | Depends on | What it does |
|---|---|---|
| `preflight` | — | The release preflight, then the base release: its tag still names the pinned commit, the checkout descends from it, and its cdk.json deploys the same project and Regions. |
| `baseline` | `preflight` | Reused. |
| `prepare` | `preflight` | Builds the workspace, installs the base `gco`, and synthesizes the base app; its stacks must be this checkout's targets and must all carry the run tag. |
| `deploy` | `baseline`, `prepare` | Requires the image mirror's repositories in the baseline, runs the base `gco stacks deploy-all`, then adopts what it created by run tag. |
| `sentinels` | `deploy` | Writes the sentinel through the base release's API. |
| `upgrade` | `sentinels` | Tags the candidate in the mirror, checks the plan with `gco upgrade --check`, then runs the base `gco upgrade` and adopts the recreated stacks. |
| `topology` | `upgrade` | Reused, against the upgraded deployment. |
| `verify-upgrade` | `topology` | The clone is at this commit with cdk.json intact, the control plane kept its stack IDs, every workload stack is a new generation, and the sentinel came back unchanged. |
| `destroy` | `deploy` | Reused. |
| `final-inventory` | `destroy` | Reused. |

The base release's upgrade engine is what runs. A change to `cli/upgrade.py`
in this checkout is exercised by the *next* release's run, when this checkout
is the base; the candidate's app, charts, and manifests are what the upgrade
deploys, and those are exercised now.

## Why ownership works differently here

The release harness owns a stack through the change set it prepared itself,
checkpointed before CloudFormation executed it. Here the stacks are created by
a `gco` subprocess, so there is no such change set. `ownership/stacks.py` in
the sibling package therefore has a second authority, `run-tag-adoption`,
recorded by `_adopt_run_tagged_stacks`: a target stack is adopted when it
carries this run's `GcoLiveValidationRun` tag and CloudFormation reports it
created after the phase that created it began (the phase checkpoints the AWS
server time from an STS response first). A stack the upgrade recreates is a
new generation under the same name. It is accepted only for the workload
stacks `gco upgrade --check` named, and only once the previous generation is
deleted; the previous one moves to `replaced_generations`, so the EKS key and
log groups it retained stay attributable to the run (`_owned_stack_ids` is what
the KMS and log-group validators consult). Every other rule is unchanged:
destroy re-checks the exact stack ID and tag before each delete, and a stack
without the tag is never adopted.

A recreated stack keeps its fixed resource names, so the new generation derives
some log groups under names the replaced one had checkpointed: the EKS
cluster's control-plane and Container Insights groups, and the default group of
every Lambda function with an explicit name. `_checkpoint_owned_log_groups`
hands such a record to the new generation (`_carry_log_group_across_generations`).
If the recorded group outlived the old stack, the record is rebound in place and
its earlier binding kept under `stack_generations`. If the group is gone
(`gco`'s teardown deletes the implicit log groups of the stacks it destroys),
stable absence or a stable different generation proves it, since a deleted
generation never returns; the record moves to `superseded_log_groups` and the
name is checkpointed afresh from the new generation. Only a replaced
generation's record can be handed over, with every field but the stack binding
unchanged; anything else is still an ownership change and fails closed.

The relaxation is gated on the settings class (`allows_run_tag_adoption` is a
class attribute), so the release and example harnesses cannot record or honor
an adopted stack.

ECR has no such second authority. The release harness owns a repository its
image mirror creates through the `on_ecr_repository_created` callback, which a
`gco` subprocess cannot call. So `deploy` first requires every repository
either release's mirror copies into (the `configured-mirror` targets of both
checkpointed image graphs) to be in the baseline: the mirror can then only add
tags, which `_checkpoint_new_ecr_images` records as retained deltas exactly as
it does for a release run. The base graph's mirror targets come from the base
clone's own `charts.yaml`, because `_expected_ecr_images` reads the chart
catalogue of the checkout it is given.

The run tag reaches the stacks through the base checkout's cdk.json:
`context.tags` is applied to every stack by the app, and `gco upgrade`
restores cdk.json byte for byte after its checkout, so the redeploy carries the
tag too. `prepare` proves this from the synthesized assembly before anything is
deployed. The run-scoped context the sibling passes as `--context` flags (EFS
automatic backups off, provider log groups retained for the harness to delete)
lives in the same file for the same reason.

## The workspace

`<report-dir>.workspace` by default, never inside the checkout or the report
directory:

- `mirror.git` — a bare repository with the base tag and the candidate commit
  only, borrowing objects from the operator's repository through git
  alternates. The synthetic release tag (the base's next patch version) is
  created here, so the operator's repository never gains a tag;
- `base` — the clone `gco upgrade` runs in, `origin` pointing at the mirror;
- `venv` — the base `gco`, installed editable with the `cdk` extra and pinned by
  the base release's `requirements-lock.txt`;
- `logs` — one log per command.

Every command runs under `umask 022` with the venv first on `PATH`, no
inherited `PYTHONPATH` or git plumbing variables, and `GCO_*` project and
Region settings pinned to the harness's.

## Phases and resume

`deploy` and `upgrade` each run one long subprocess inside a phase
(`checkpoint.state.upgrade_validation.phases`). The phase is checkpointed
before its command starts. After the command, whether it succeeded, failed, or
timed out, the phase adopts what it left, reconciles ownership, and checkpoints
the retained KMS keys and log groups while their stacks stand.

A phase the harness itself was interrupted in (`SIGTERM`, a crash, a lost
terminal) is adopted by `preflight` on `--resume` before the release preflight
reconciles ownership, and is marked interrupted. The action then fails instead
of rerunning the command, and the runner's guaranteed cleanup tears the run
down: an interrupted upgrade is a failed validation. The phase checkpoints its
command's process ID, and a harness killed outright (`kill -9`) leaves that
command running on its own, so `preflight` refuses to adopt anything while a
process with that ID still runs the workspace's `gco`.

## Testing your change

```bash
PYTHONPATH=. python -m pytest -q tests/test_upgrade_validation.py \
  tests/test_live_validation_adoption.py
```

The first covers this package against fakes and a real temporary git
repository; the second covers the shared adoption primitive. Neither touches
AWS. `python -m scripts.upgrade_validation --list-actions` prints the registry.
