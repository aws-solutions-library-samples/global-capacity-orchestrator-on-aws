# Example Validation Harness

This package proves the example gallery works. Every file under `examples/` is
parsed, checked against the transport gates its documented submission path
actually enforces, and — in the live half — submitted the way the docs tell a
user to submit it, then watched until it either meets its success criteria or
fails.

[`docs/EXAMPLE_VALIDATION.md`](../../docs/EXAMPLE_VALIDATION.md) is the
operator runbook: when validation is required, what the two halves prove, and
how to run them. **This** file is the developer guide: how the code is
organized and where a new example, submission path, or criterion belongs.

The harness is a thin layer over its sibling: `registry.py` reuses
`scripts/live_release_validation`'s preflight, baseline, deploy, destroy, and
final-inventory actions verbatim and adds only two of its own. Read
[that package's README](../live_release_validation/README.md) first for the
run model (identity gates, checkpoints, ownership, reporting); everything
below is what this harness adds on top.

## Table of Contents

- [Layout](#layout)
- [How a run executes](#how-a-run-executes)
- [The static half is the CI gate](#the-static-half-is-the-ci-gate)
- [Adding an example](#adding-an-example)
- [Adding a submission path](#adding-a-submission-path)
- [Adding a success criterion](#adding-a-success-criterion)
- [Adding a setup driver](#adding-a-setup-driver)
- [Mutations are disclosed, never silent](#mutations-are-disclosed-never-silent)
- [Layering rules](#layering-rules)
- [Testing your change](#testing-your-change)

## Layout

| Path | Responsibility |
|---|---|
| `__main__.py` | CLI entry (`python -m scripts.example_job_validation`): identity flags, example selection, `--static-only`, checkpoint/resume. Mirrors the sibling harness's argument surface; `gco examples validate` is the no-prompt wrapper around it. |
| `registry.py` | The ordered action registry: five reused actions plus `static` and `examples`. Single source of truth for `--actions`, held in lockstep with the contract table in `docs/EXAMPLE_VALIDATION.md`. |
| `models.py` | `ExampleRunSettings` — the sibling's `RunSettings` plus the selection and the parallelism cap. The helm charts and optional features the run needs are *derived* from the selection here, and the selection is part of `identity()` so a resume cannot quietly validate a different set. |
| `specs.py` | One `ExampleSpec` per example: how it is submitted, what infrastructure it needs, when it counts as passed, and which mutations are applied first. Declarative on purpose. |
| `static_checks.py` | The offline half: parse, spec/catalog/directory symmetry, transport acceptance, target namespaces, resource-governance fit. No AWS, no cluster. |
| `actions.py` | The two new action handlers: `action_static` and `action_examples` (per-example lifecycle, capacity skips, parallelism). |
| `drivers.py` | The per-example machinery `actions.py` orchestrates: submission through the real CLI or `kubectl`, the success-criteria waiters, setup drivers, and cleanup. Returns evidence dictionaries for the report. |
| `kube.py` | Cluster access shared with sibling harnesses: SSM tunnel, kubeconfig handling (an isolated path when asked, so a run never rewrites `~/.kube/config`), and kubectl execution. |

## How a run executes

`registry.py` returns the actions in dependency-safe order, and the sibling's
`runner.py` executes them, checkpointing after each:

| Action | Depends on | What it does |
|---|---|---|
| `preflight` | — | Reused: exact git/account/configuration identity. |
| `static` | — | The offline checks. Runs before anything is deployed, so a malformed example costs seconds rather than a full deploy. |
| `baseline` | `preflight` | Reused: protected CloudFormation and ECR baselines. |
| `deploy` | `baseline`, `static` | Reused, with the twist below: the topology plus whatever the *selected* examples require. |
| `examples` | `deploy` | Each selected example through its documented path, in parallel up to `--max-parallel`. |
| `destroy` | `deploy` | Reused: tear down everything this run owns. |
| `final-inventory` | `destroy` | Reused: prove zero residue and an untouched baseline. |

`--max-parallel` caps how many examples the `examples` action runs at once
(`0`, the default, means all selected; `1` is serial). It is deliberately not
part of the run identity: pacing is not what is being validated, so a
checkpointed run may resume with a different value.

The `deploy` dependency on `static` is the ordering that matters most here:
selection decides infrastructure. `ExampleRunSettings` derives the helm charts
and optional features the chosen examples need
(`required_helm_overrides` / `required_feature_overrides` in `specs.py`) and
threads them into every CDK invocation as context, so validating one KEDA
example does not deploy the whole optional surface — and validating it *does*
deploy KEDA. Those derived features are part of `identity()`, so a resume
against a differently-provisioned deployment is refused rather than silently
accepted.

## The static half is the CI gate

`static_checks.run_static_checks` needs no credentials, so
`tests/test_example_job_validation.py` runs it as an ordinary test on every PR.
That is deliberate: a change to `examples/` that breaks a documented contract
fails the PR that made it, not a maintainer's next live run.

It enforces, per example:

- **Parse** — every document is valid YAML with the fields its kind requires.
- **Symmetry** — the `examples/` directory, `EXAMPLE_SPECS`, and the `gco_mcp`
  `EXAMPLE_METADATA` catalog name exactly the same set, and agree on the
  submission path. Three-way, so adding an example to one place and forgetting
  the others fails.
- **Transport acceptance** — for examples documented to travel the API or SQS
  path, every document clears the exact gates the deployed services enforce.
  Not a reimplementation of them: it calls the services' own
  `validate_resource_kind` and `validate_image_sources`, so the check cannot
  drift from what production accepts.
- **Governance fit** — requests and limits fit the shipped ResourceQuota,
  LimitRange, and per-manifest caps (read from `gco.stacks.constants`, again
  the deployed values), so an example cannot be rejected at admission on a
  stock deployment.
- **Spec shape** — every spec names a known submission path, criterion, and
  setup driver, so a typo fails here rather than mid-run.

Run it locally with `python -m scripts.example_job_validation --static-only`
(add `--examples <stem>` to narrow it), or through pytest.

## Adding an example

1. Add the manifest to `examples/`.
2. Add its `ExampleSpec` to `EXAMPLE_SPECS` in `specs.py`: submission path,
   success criteria, any `helm_enabled_overrides` / `feature_enabled_overrides`
   it needs, capacity requirements, and setup drivers.
3. Add its entry to `EXAMPLE_METADATA` in `gco_mcp/resources/docs.py`, the
   catalog the MCP tools serve. `static_checks.py` reads that literal with
   `ast` rather than importing it, so the symmetry check needs no MCP runtime.
4. Run the static checks. The symmetry check names anything you missed.

An example with no live submission path (a companion artifact — a config file,
a fragment another example includes) still needs a spec: mark it `COMPANION`
and the harness records it as documented-not-run rather than silently ignoring
it.

## Adding a submission path

Submission paths are constants in `specs.py` (`SUBMIT_DIRECT`, `SUBMIT_SQS`,
`SUBMIT_API`, `DAG_RUN`, `KUBECTL_APPLY`, `COMPANION`) with `submit_example`
in `drivers.py` dispatching on them. A new path means: add the constant to
`SUBMISSION_PATHS`, teach `submit_example` to travel it **the way the docs
tell users to** (shell out to the real `gco` command or `kubectl`; never
reimplement the submission), and extend `check_transport_acceptance` in
`static_checks.py` if the new path has gates of its own.

## Adding a success criterion

Criteria are constants in `specs.py` mapped to waiter functions by
`CRITERIA_WAITERS` in `drivers.py`. A waiter polls the cluster and either
returns evidence or raises `ExampleValidationError` with the diagnostics that
explain the failure (pod status, events, admission rejection). Fail fast on
anything waiting cannot heal — a rejected pod or a missing CRD is a failure
now, not in five minutes.

## Adding a setup driver

Some examples need something to exist before they can succeed: a queue with
messages in it for the KEDA scaler to see, a corpus to search, a trainer
runtime or an MLflow server to be Ready. Those are setup drivers, named by
`spec.setup_driver` and listed in `KNOWN_SETUP_DRIVERS` in `drivers.py`.

The registry is explicit on purpose: a spec naming a driver that does not
exist fails the offline pin test, not a live run forty minutes in. Add the
name to the frozenset, implement it next to its siblings (a context manager
that provisions and tears down, like `KedaDemoQueue`, or a readiness waiter
like `wait_mlflow_ready`), and make sure whatever it creates is cleaned up
even when the example fails.

## Mutations are disclosed, never silent

Some examples cannot run verbatim in a validation account: a gated
HuggingFace model, a value that must be swapped for a CI-safe one. Those are
declared as `mutations` on the spec, applied by `apply_mutations` to a
disclosed temp copy (`write_temp_manifest`), and reported. The shipped example
is never edited, and the report always states what ran instead of what ships.
`REMOVE_VALUE` deletes a key rather than replacing it.

## Layering rules

Imports flow one way, from the entry point down to the data, and
`tests/test_example_job_validation.py` enforces it:

```text
__main__.py → registry.py → actions.py → drivers.py → kube.py
                                      ↘            ↘
                                        static_checks.py → specs.py
                                                            ↑
                                              models.py ────┘
```

- `specs.py` imports nothing from this package. It is data.
- `static_checks.py` reaches only for `specs.py` inside the package. It may
  import production validators from `gco.*` — that reuse is the point — but
  never a driver, a cluster session, or boto3. Keeping it that way is what
  lets CI run it on every PR.
- `drivers.py` and `actions.py` are the only modules that touch a cluster,
  through `kube.py`.
- Reuse from `scripts.live_release_validation` is one-directional: this
  package imports that one, never the reverse. If a change would need the
  reverse, it belongs in the sibling package instead.

## Testing your change

`tests/test_example_job_validation.py` is the whole offline suite: the static
checks as a CI gate, plus the harness plumbing (spec enumeration, derived
overrides, action registry order and dependencies, argument surface, mutation
application, waiters and cleanup against a scripted kubectl, parallelism).
Every AWS, Kubernetes, and subprocess boundary is stubbed, so the suite is
hermetic.

Add coverage next to the layer you touched, then:

```bash
pytest tests/test_example_job_validation.py -q
python -m scripts.example_job_validation --static-only
```

A live run is required only for changes to the live half; see the runbook for
the identity flags and the safety posture it demands.
