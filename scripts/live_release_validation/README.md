# Live Release Validation Harness

This package is the end-to-end test for GCO. It deploys the checked-in
topology into a disposable AWS account, exercises the real job paths, tears
everything down, and proves the account came back clean.

`docs/LIVE_RELEASE_VALIDATION.md` is the operator runbook: when validation is
required, the safety model, and how to run it. **This** file is the developer
guide: how the code is organized and where a new check belongs.

## Table of Contents

- [Layout](#layout)
- [How a run executes](#how-a-run-executes)
- [Adding a check: decide the scope first](#adding-a-check-decide-the-scope-first)
- [Extending an existing action](#extending-an-existing-action)
- [Adding a new action](#adding-a-new-action)
- [Adding a new owned resource type](#adding-a-new-owned-resource-type)
- [Layering rules](#layering-rules)
- [Testing your change](#testing-your-change)

## Layout

| Path | Responsibility |
|---|---|
| `registry.py` | The ordered action registry: names, descriptions, dependencies, handlers. Single source of truth for `--actions`. |
| `runner.py` | Executes selected actions, checkpoints after each, and guarantees cleanup + reporting on every exit path (including signals). |
| `models.py` | `RunSettings`, `RunCheckpoint`, `RunContext`, `ActionResult`, `ValidationReport`, and the owner-only artifact I/O. |
| `volume_scenario.py` | The pure E2E EBS volume-scenario contract: which cases exist, which of them a checkpoint identity may be fenced to, and each case's isolated run identity. |
| `scenario_driver.py` | Expands `--volume-scenario both` into two sequential, separately fenced runs (`<run-id>-volumes-retain-override`, then `<run-id>-volumes-delete`), each with its own report directory, checkpoint, identity, and complete deploy/destroy lifecycle. |
| `actions/` | One module per action. This is the "test case" layer. |
| `checks/` | Reusable validation helpers (polling, waiting, payload validation) shared by actions. |
| `ownership/` | Durable proof of what this run created and may therefore destroy. |
| `cleanup/` | Deletion of exactly those proven-owned resources. |
| `protected.py` | The ownership boundary: identity matching that keeps pre-existing account resources untouchable. |
| `context.py` | Run identity helpers: git SHA/branch, topology profile, Region selection. |
| `constants.py` | Tags, labels, and tuning constants shared across modules. |
| `inventory/` | Read-only AWS inventory collection and baseline comparison, split per concern (`stacks`, `ecr`, `scanners`, `project`). |
| `manifests/` | The Kubernetes Job manifests the job-path actions submit. |

Every action module maps to exactly one registry entry, so the module name
matches the action name printed in the run log. The one deliberate exception is
`actions/jobs.py`, which holds both `api` and `sqs`: they are the same Job
lifecycle over two different transports, and splitting them would duplicate the
lifecycle rather than clarify it.

The `volume-inventory` action (`actions/volume_inventory.py`, live observation
helpers in `checks/volumes.py`, identity fencing in `ownership/volumes.py`) runs
straight after `topology` and only for an explicitly selected
`--volume-scenario` case. It records what the volume-policy assertions are later
measured against: each PVC's identity and requested size, its bound PV's
identity/CSI driver/`volumeHandle`, and the normalized EBS facts for the volumes
those PVCs produced — reusing `cli.volume_cleanup.normalize_volume_snapshot` so
the harness records exactly what production decides against. PVCs that produced
no EBS volume get an explicit non-participation reason and the rest continue.
Every Region passes the scenario authorization gate before any EKS or EC2 call,
and each Region's evidence is persisted as it is observed.

`ownership/volume_targets.py` turns that inventory into the strict destroy-time
targets. Volume cleanup runs after the stack and cluster are gone, but the
identity that authorizes it — exact CloudFormation stack ARN, Region, EKS
cluster physical ID, exact cluster tag key, and the recorded volume identities —
is readable only while they still exist, so `destroy_deployment` captures and
checkpoints it before the first deletion. A missing or ambiguous target identity
is persisted as a blocked target and then raised, so teardown stops rather than
destroying evidence the scenario still needed; only complete targets are
reconstructed for the shared `StackManager` cleanup helper.

`ownership/volume_requests.py` decides *what command* that teardown exercises,
through the same command-aware resolver Click uses: the retain-override case
supplies exactly the inputs of `gco stacks destroy-all -y --retain-volumes`, and
the delete case supplies exactly the inputs of `gco stacks destroy-all -y` with
`delete_volumes=False` — which is what proves the implicit-delete path needs
neither a `--delete-volumes` flag nor a second volume confirmation. The one
resolved request is passed into `destroy_orchestrated` so every exact regional
target shares it, and the same module holds the completion barrier: teardown is
marked complete only once every published `ebs-volumes` callback is durable in
the persisted checkpoint, one per captured strict target and each carrying this
run's policy.

Those callbacks come from the code under test, so they cannot also be the proof
that it was right. `checks/volume_outcomes.py` is the independent half: it
re-describes the exact checkpointed volume IDs in their own Region and decides
from live EC2 facts whether the case actually happened — retain-override needs
every recorded volume still present with its exact recorded tag and a retention
outcome, delete needs every volume the pre-destroy inventory showed as owned,
`available`, and detached to be *absent* (proved only by the exact not-found
error) while ineligible volumes remain with a safety outcome. Any disagreement is
persisted and then raised, before teardown can be marked complete.

Only once that evidence is durable and verified does
`cleanup/volume_fixtures.py` delete what the retain case deliberately kept, so a
passing validation does not leave a recurring bill. It is fenced four ways: only
after verified retention evidence, only for exact checkpointed identities, only
with `--confirm-ebs-fixture-cleanup` (the `fixture_cleanup=True` gate in
`ownership/volumes.py`), and only through `VolumeCleanupService.delete_candidates`
so the same just-in-time recheck operators get applies to fixtures too. It never
raises to hide a leftover: `volume_residual_inventory` re-runs the observation
during `final-inventory`, which fails the run on any recorded volume that still
exists, cannot be proved gone, or was never authorized for deletion.

The `schedulers` action (`actions/schedulers.py`, enablement resolution in
`checks/schedulers.py`) runs one scheduling-gated probe Job per enabled batch
scheduler through the shared API-transport lifecycle in `checks/jobs.py`
(`_run_api_transport_lifecycle`, also the body of the `api` action). The probe
manifests live in `manifests/*-smoke-job.yaml`; each completes only if its
scheduler actually scheduled it (foreign `schedulerName`, Kueue queue
admission, or a real slurmrestd round trip). Off-by-default schedulers are
skipped with their configuration source unless the run passes
`--optional-schedulers` — which the runner threads to every CDK invocation as
the `helm_enabled_overrides` context so the deployed chart set, the applier's
gated manifests, and the probes all resolve enablement identically.

## How a run executes

`runner.py` resolves the requested actions (expanding dependencies), then for
each one in registry order:

1. Skips it if the checkpoint already records a pass (except `preflight`, which
   always re-verifies identity before anything destructive is permitted).
2. Calls the handler with the shared `RunContext`.
3. Persists the checkpoint and rewrites both reports — pass or fail.

Whatever happens, if a deploy was ever attempted the runner then runs
guaranteed cleanup (`destroy_deployment`) and `final-inventory`, so an
interrupted run still tears down and still reports.

An action handler is just:

```python
def action_my_check(ctx: RunContext) -> dict[str, Any]:
    """One sentence describing the contract this action enforces."""
    ...
    return {"evidence": ...}  # JSON-able; lands in the checkpoint and reports
```

Raise on failure. Return the evidence you want a reviewer to be able to read
six months later. Never `print` a decision that belongs in the returned
evidence.

## Adding a check: decide the scope first

Use the narrowest option that fits.

| Situation | Where it goes |
|---|---|
| A new assertion about infrastructure an existing action already inspects | Extend that action (or a `checks/` helper it calls) |
| A behavior that needs no AWS account — config, template, or pure logic | **Not here.** Add a normal unit test under `tests/`; ordinary CI must stay offline |
| A new independently selectable phase of the run, with its own dependencies and its own pass/fail meaning | A new action |
| A new AWS resource type this run creates and must delete | `ownership/` proof + `cleanup/` deletion (see below) |

The bar for a new action is real: every action lengthens a validation run that
already costs real money and roughly two hours of wall clock. If your check
only makes sense once `topology` has passed and shares its evidence, it
probably belongs inside `topology`.

## Extending an existing action

1. Put reusable polling/validation logic in `checks/` — not inline in the
   action — if anything else could plausibly need it.
2. Add the new evidence to the dict the action returns. Keep keys stable;
   report readers and the structure tests treat them as an interface.
3. Cover the new logic in `tests/test_live_release_validation.py` with mocked
   clients. Patch helpers with `patch_live_validation_helper` (see
   [Testing your change](#testing-your-change)).

## Adding a new action

Four steps, all mechanical:

1. **Create `actions/<name>.py`** with one `action_<name>` handler and a module
   docstring naming the action.
2. **Export it** from `actions/__init__.py` (`__all__` is alphabetized).
3. **Register it** in `registry.py` with its dependencies. Order in the tuple
   is execution order; dependencies must appear earlier.
4. **Document it** in the contract table in `docs/LIVE_RELEASE_VALIDATION.md`,
   with the same name and the same dependency.

`tests/test_live_release_validation_structure.py` fails if you skip step 2, 3,
or 4, so a half-registered action cannot merge.

If the action creates AWS resources, it must also checkpoint ownership before
creating them and register teardown — otherwise `final-inventory` will
correctly fail the run for leaving residue.

## Adding a new owned resource type

This is the safety-critical path. The rule the whole harness is built around:
**a matching resource name is never proof of ownership.**

1. **Prove creation** in `ownership/`: checkpoint the resource's immutable
   identity (ARN, creation time, UID, or generation) *plus* the run tag, before
   or as it is created. Read `ownership/ecr.py` for the simplest complete
   example.
2. **Re-validate at delete time** in `cleanup/`: re-read the live resource and
   confirm the checkpointed identity still matches before issuing any delete.
   Where AWS supports it, make the delete itself conditional on the run's tags
   (`ownership/cleanup_role.py` does this for log groups via a scoped session
   policy) so a race cannot destroy a foreign resource.
3. **Account for it** in `actions/final_inventory.py`, either as "must be
   absent" or as an explicitly accepted retained resource.
4. **Keep it out of the protected baseline** by teaching `protected.py` the
   resource's identity shape, so a pre-existing account resource of the same
   type can never be matched as run-owned.

## Layering rules

Imports flow one way:

```text
actions/   ->  checks/, cleanup/, ownership/, protected, context, constants
cleanup/   ->  checks/, ownership/, protected, context, constants
checks/    ->  ownership/, protected, context, constants
ownership/ ->  protected, context, constants
inventory/ ->  inventory/ only (read-only, no harness state)
registry   ->  actions/
```

`cleanup/` may import `checks/` because deleting a workload reuses the same
Job primitives the checks use; nothing in `checks/` imports `cleanup/`, which is
what keeps that edge acyclic.

`ownership/`, `checks/`, and `cleanup/` must never import from `actions/`, and
nothing except `runner.py` and `__main__.py` imports `registry`. The structure
tests enforce both directions, which is what keeps the import graph acyclic as
the harness grows.

## Testing your change

The harness has two layers of test, and both run in ordinary offline CI:

- **`tests/test_live_release_validation.py`** — behavior, with mocked AWS
  clients. This is where a new check earns its coverage.
- **`tests/test_live_release_validation_structure.py`** — architecture:
  registry/docs consistency, module-to-action mapping, layering, module size
  ceiling, and docstring presence. These fail fast on a structural mistake so
  a reviewer never has to catch it by eye.

When a test needs to replace a harness helper, use the shared patcher rather
than `patch.object` on a specific module:

```python
from tests._live_validation_patching import patch_live_validation_helper

with patch_live_validation_helper("_register_job", return_value=record) as mock:
    ...
```

It installs one shared mock into every module that binds the name, so moving a
helper between modules stays a pure refactor and a stale patch target fails
loudly instead of silently running production code against a real account.

Run them together:

```bash
pytest tests/test_live_release_validation.py \
       tests/test_live_release_validation_structure.py
```

Ordinary CI must never invoke the live harness. `runner.require_local_execution`
refuses to run under `GITHUB_ACTIONS`, and that guard is itself tested.
