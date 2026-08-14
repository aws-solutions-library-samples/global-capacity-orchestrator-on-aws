# Example Job Validation

Every manifest under [`examples/`](../examples/) is a promise: submit it the
way its header documents and it works. `gco examples validate` proves that
promise — it stands up real infrastructure, runs every selected example
through its **documented** submission path, verifies workload-specific
success criteria, cleans up, tears the infrastructure down, and writes a
per-example report. The harness lives in `scripts/example_job_validation/`
and reuses the [live release validation](LIVE_RELEASE_VALIDATION.md)
machinery (preflight, baseline, deploy, destroy, final inventory,
checkpoint/resume, private reports).

## Table of Contents

- [When You Must Run It](#when-you-must-run-it)
- [The Two Halves](#the-two-halves)
  - [Static (offline, CI-enforced)](#static-offline-ci-enforced)
  - [Live (deploy → run → destroy)](#live-deploy--run--destroy)
- [How Each Example Runs](#how-each-example-runs)
- [Reports](#reports)
- [Scoping and Iteration](#scoping-and-iteration)

## When You Must Run It

| You changed... | Required validation |
|---|---|
| Nothing under `examples/` | Nothing extra — CI still runs the static checks |
| An example's comments/docs only | `gco examples validate --static-only` (seconds, offline; CI enforces the same checks) |
| An example's **behavior** (image, command, resources, labels, scheduler, target) | Static checks **plus** a live run scoped to it: `gco examples validate --examples <name> ...` |
| Added or removed an example | Live run for it, plus a spec entry in `scripts/example_job_validation/specs.py` and a catalog entry in `gco_mcp/resources/docs.py` (three-way symmetry is CI-enforced) |
| Platform behavior examples depend on (transports, schedulers, storage, namespaces) | Full live run: `gco examples validate` with no selection |

The PR template's Testing section asks which of these applied; reviewers
should expect a sanitized summary (run id, SHA, per-example table) for any
live run, exactly like live release validation.

## The Two Halves

### Static (offline, CI-enforced)

```bash
gco examples validate --static-only            # every example
gco examples validate --static-only --examples gpu-job
```

No AWS access. For every example: the YAML parses; documents documented to
travel the API/SQS transports clear the exact deployed gates (kind/GVK
allowlist, trusted image sources); namespaced documents target a
provisioned workload namespace; every gco-jobs workload fits the deployed
resource governance (per-container `LimitRange` ceilings, per-manifest
caps, and the namespace `ResourceQuota`, evaluated against the same
defaults the stack deploys — a manifest that admission would reject
forever fails here in seconds instead of burning a live timeout); and the
spec registry, the `examples/` directory, and the `gco_mcp`
`EXAMPLE_METADATA` catalog stay in three-way symmetry (including each
entry's documented submission command).
`tests/test_example_job_validation.py` runs the same checks in CI, so
drift fails the PR that introduces it.

### Live (deploy → run → destroy)

```bash
gco examples validate \
  --expected-account 123456789012 \
  --i-understand-this-deploys-and-destroys-infrastructure \
  --confirm-kms-key-deletion            # add --examples/--skip-examples to scope
```

Action pipeline: `preflight → static → baseline → deploy → examples →
destroy → final-inventory`. Consent, identity verification (account, SHA,
branch, clean worktree), checkpoint/resume, KMS-deletion authorization,
and report privacy all behave exactly as documented for
[live release validation](LIVE_RELEASE_VALIDATION.md). A failed example
never skips teardown — `destroy` and `final-inventory` run regardless.

Per-run enablement is **derived from the selection**: examples that need
off-by-default schedulers thread `helm_enabled_overrides` (slurm,
yunikorn) and examples that need optional infrastructure thread
`feature_enabled_overrides` (`aurora_pgvector`, `valkey`, `fsx_lustre`, `vector_store`)
into every CDK invocation of the run — cdk.json is never rewritten, so the
clean-worktree preflight holds.

Within the `examples` action, all selected examples run **in parallel** by
default: each is self-contained (own workload names, own temp manifest,
own cleanup), so node provisioning and image pulls — the dominant costs —
overlap instead of serializing. `--max-parallel N` throttles the pool
(`1` restores serial execution) and may differ between a run and its
resume. While peers hold namespace quota, `exceeded quota` admission
rejections are expected and retried by Kubernetes; only permanent
rejections (for example a container over the `LimitRange` ceiling) fail an
example immediately instead of waiting out its timeout.

## How Each Example Runs

The spec registry (`scripts/example_job_validation/specs.py`) declares one
entry per example: documented submission path, success criteria, derived
enablement, capacity gates, timeouts, and any disclosed mutations.

| Submission path | Used by | Success criteria |
|---|---|---|
| `gco jobs submit` (API) | inferentia, trainium | Job completes |
| `gco jobs submit-sqs` | simple, gpu, sqs-job-submission, kubeflow-trainjob | Job completes / TrainJob condition Complete (with per-node gang counts) |
| `gco jobs submit-direct` | storage/data examples, efa training, inference pairs, vector-store-search, mlflow-tracking | Job completes / Deployment Available + Service endpoints |
| `gco dag run` | pipeline-dag (+ its two step files) | DAG run exits 0, steps complete |
| `kubectl apply` (documented for CRDs) | kueue, volcano, yunikorn, slurm, ray, keda, multi-gpu, model-download | Jobs complete / vcjob Completed / RayCluster ready / ScaledJob spawns Jobs |

`kubectl` reaches the PRIVATE EKS endpoint through the CLI's own
SSM-tunnel machinery (`gco cluster tunnel --via-ssm auto` internals): the
harness provisions the ephemeral bastion, points kubeconfig at the tunnel
(`tls-server-name` pinned to the real endpoint host), and tears the
bastion down with the session — so `gco jobs submit-direct`, which shells
out to kubectl, works unmodified too.

Special drivers, fully reverted afterwards (a spec naming a driver the
dispatcher does not implement fails in CI and at dispatch — never a silent
skip):

- **keda-scaled-job** — creates a disposable demo SQS queue, seeds
  synthetic messages, grants the KEDA operator read-only queue metrics via
  a queue policy (the example's documented prerequisites), substitutes the
  placeholder `queueURL`, requires KEDA to spawn observer Jobs, then
  deletes the queue.
- **vector-store-search-job** — runs the documented prerequisite verbatim
  (`gco vector ingest --demo --wait`), records exactly which corpus objects
  were uploaded, and reverts precisely those afterwards: the DynamoDB chunk
  items per recorded source key, then the S3 objects. A pre-existing user
  corpus in the same table is never touched.
- **kubeflow-trainjob** — waits for the TrainJob CRD and the shipped
  `torch-distributed` runtime before submitting (deploy-time artifacts;
  nothing to revert).
- **mlflow-tracking-job** — waits for the tracking server Deployment to be
  Available first, since its backend volume lands one applier pass after
  the chart on a fresh install (readiness wait; nothing to revert).

Disclosed mutations: inference examples whose default model is
HuggingFace-gated (vLLM's Llama 3.1, TGI's Mistral) are validated with the
ungated `facebook/opt-125m` substituted (and TGI's AWQ quantization flag
removed — it is checkpoint-specific). The serving path itself runs
unchanged, and every mutation appears in the report row. Examples whose
defaults are ungated (SGLang's Phi-3.5) run verbatim.

Capacity-gated examples (`efa-distributed-training` on P-family,
inferentia/trainium on Inf/Trn) check the account's service quota first
and record an explicit **skipped** row with the quota evidence when it is
zero — a skip is never silent.

## Reports

`~/gco-example-job-validation-reports/<run-id>/` receives
`example-job-validation.{json,md}` plus `checkpoint.json`. The JSON
carries a per-example row (status, duration, submission command, disclosed
mutations, criteria evidence, cleanup proof). Reports contain
account-specific identifiers — share sanitized summaries only, never the
raw files.

## Scoping and Iteration

```bash
# One example, full lifecycle (deploy + destroy included):
gco examples validate --examples slurm-cluster-job ...

# Everything except the long GPU training examples:
gco examples validate --skip-examples efa-distributed-training,multi-gpu-training ...

# Resume an interrupted run (exact identity required):
gco examples validate --resume --run-id <id> --report-dir <dir> ...
```

`--examples` also narrows the derived enablement: selecting only
`valkey-cache-job` deploys with Valkey forced on but leaves Aurora, FSx,
and the optional schedulers at their cdk.json defaults.
