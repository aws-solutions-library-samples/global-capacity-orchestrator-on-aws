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

This page is the operator runbook. To change the harness itself — add an
example, a submission path, a success criterion — read
[`scripts/example_job_validation/README.md`](../scripts/example_job_validation/README.md),
the developer guide to its layout and layering.

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
provisioned workload namespace (an Argo CD `Application` instead stays inside
the fence: the `argocd` namespace, the `gco-tenants` project, the in-cluster
server and a tenant destination); what an `Application` syncs from this
repository, a kro ResourceGraphDefinition composes or a Crossplane
Composition renders lands in a workload namespace and pulls from trusted
image sources; every gco-jobs workload — including those — fits the deployed
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
off-by-default charts thread `helm_enabled_overrides` (`argocd`,
`crossplane`, `slurm`, `yunikorn`), examples that need optional
infrastructure thread `feature_enabled_overrides` (`aurora_pgvector`,
`valkey`, `fsx_lustre`, `vector_store`), and examples that need an
[EKS Capability](EKS_CAPABILITIES.md) thread `eks_capabilities_overrides`
(`kro`; `ack`, with `AmazonSQSFullAccess` on the capability role for the SQS
example) into every CDK invocation of the run — cdk.json is never rewritten,
so the clean-worktree preflight holds.

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
| `kubectl apply` (platform add-ons) | argocd-gitops-job, crossplane-batch-job, kro-batch-job (+ their companion API files), ack-sqs-queue | Application Synced + Healthy at the pinned commit with its Git Jobs complete / the composed Job completes / `ACK.ResourceSynced` and the queue resolves in SQS |

`kubectl` reaches the PRIVATE EKS endpoint through the CLI's own
SSM-tunnel machinery (`gco cluster tunnel --via-ssm auto` internals): the
harness provisions the ephemeral bastion, points kubeconfig at the tunnel
(`tls-server-name` pinned to the real endpoint host), and tears the
bastion down with the session — so `gco jobs submit-direct`, which shells
out to kubectl, works unmodified too.

The session also keeps that tunnel carrying traffic, because a Session
Manager port-forward can stall with its local listener still accepting
connections. One run lost seven examples that way: no TLS handshake
completed for over an hour, five `submit-direct` calls failed on `TLS
handshake timeout`, and two watchers read submitted Jobs as missing until they
timed out. Now a watchdog completes a TLS handshake through the tunnel every
30 seconds. After two failures in a row, or at once when the session process
exits, it reopens the session on the same local port through the same
bastion, so the kubeconfig stays valid. Around that:

- a kubectl call that fails on the transport and finds the tunnel broken
  reopens it at once and is repeated one time, and so is a `submit-direct`
  while none of the example's Jobs exists yet (the CLI renames a second
  submission of a Job that is still running, which would start a duplicate);
- a Job read that is not the API server's NotFound answer reports
  `unreachable` with kubectl's error, never `missing`, so neither a watcher
  nor cleanup mistakes a read it could not make for a Job that is gone;
- each example first checks the tunnel and fails at once, with the reason,
  when it cannot be reopened;
- the bastion's self-termination backstop is sized to the pending examples'
  worst case (their timeouts plus overhead, across the workers), within the
  one day a bastion accepts, instead of the two-hour default a sequential
  pass over the catalog outlasts.

The summary's `tunnel` block records that lifetime and every reopen the
session attempted (`reopens`, with the reason and the result).

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
- **argocd-gitops-job** — waits for the `gco-tenants` project, the
  application controller and the repo server, then pins the Application's
  `targetRevision` to the commit under validation (a disclosed mutation, so
  the run syncs exactly the fixture it tests). Passing needs the synced
  revision to equal that commit and every Job at the Git path to be one of
  the resources Argo CD manages, complete. Cleanup waits for the resources
  finalizer to delete the Job.
- **kro-batch-job** / **crossplane-batch-job** — apply the companion API
  first (`kro-batch-api.yaml` once the capability's CRD exists and the RGD
  is Active; `crossplane-batch-api.yaml` once the go-templating function is
  Healthy and the XRD Established) and wait until the new kind is served.
  Passing needs the Job composed with the instance's name to complete;
  cleanup waits for it to be deleted, then deletes the companion (also on
  failure).
- **ack-sqs-queue** — waits for the ACK capability's Queue CRD, requires
  `ACK.ResourceSynced` (`ACK.Terminal` fails at once), resolves the queue in
  SQS directly, and after cleanup requires SQS to stop resolving it: a queue
  that outlives its object is a leak.

Disclosed mutations: the inference example whose default model is
HuggingFace-gated (vLLM's Llama 3.1) is validated with the ungated
`facebook/opt-125m` substituted. The serving path itself runs unchanged, and
every mutation appears in the report row. Examples whose defaults are
ungated (SGLang's Phi-3.5) run verbatim.

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
