"""The per-example validation spec registry.

One :class:`ExampleSpec` per file under ``examples/`` (pinned by symmetry
tests against both the directory and the ``gco_mcp`` ``EXAMPLE_METADATA``
catalog). Each spec answers, for its example:

* **how** it is submitted — the exact path the docs tell users to use
  (``submit-direct`` / ``submit-sqs`` / ``dag run`` / ``kubectl apply`` /
  companion artifact with no live path);
* **what** infrastructure it needs beyond the stock deploy (optional helm
  charts via ``helm_enabled_overrides``, optional features via
  ``feature_enabled_overrides``, GPU/Neuron capacity, special setup drivers);
* **when** it counts as passed (workload-specific success criteria); and
* **which** deliberate mutations the harness applies before submission
  (e.g. replacing a gated HuggingFace model with an ungated one) — every
  mutation is disclosed in the report.

Keep this table boring and declarative: the drivers in
``checks/examples.py`` interpret it.
"""

from __future__ import annotations

from dataclasses import dataclass, field

#: Documented submission paths the drivers know how to execute.
SUBMIT_DIRECT = "cli-submit-direct"  # gco jobs submit-direct <file> -r <region>
SUBMIT_SQS = "cli-submit-sqs"  # gco jobs submit-sqs <file> --region <region>
SUBMIT_API = "cli-submit-api"  # gco jobs submit <file> --region <region>
DAG_RUN = "cli-dag-run"  # gco dag run <file> -r <region>
KUBECTL_APPLY = "kubectl-apply"  # kubectl apply -f <file> (over the SSM tunnel)
COMPANION = "companion-artifact"  # not independently runnable (data/DAG-step file)

SUBMISSION_PATHS = (SUBMIT_DIRECT, SUBMIT_SQS, SUBMIT_API, DAG_RUN, KUBECTL_APPLY, COMPANION)

#: Success-criteria kinds the drivers implement.
JOB_COMPLETES = "job-completes"  # batch/v1 Job reaches Complete
DEPLOYMENT_AVAILABLE = "deployment-available"  # Deployment Available + Service endpoints
RAYCLUSTER_READY = "raycluster-ready"  # RayCluster ready: head + minReplicas workers
VCJOB_COMPLETES = "vcjob-completes"  # batch.volcano.sh Job phase Completed
SCALEDJOB_SCALES = "scaledjob-scales"  # KEDA ScaledJob spawns >=1 Job from queue depth
TRAINJOB_COMPLETES = "trainjob-completes"  # trainer.kubeflow.org TrainJob condition Complete
DAG_SUCCEEDS = "dag-succeeds"  # gco dag run exits 0 with all steps completed
NONE = "none"  # companion artifacts: static checks only


@dataclass(frozen=True)
class ExampleSpec:
    """Declarative validation contract for one example file."""

    #: File stem under ``examples/`` (e.g. ``simple-job`` for simple-job.yaml).
    name: str
    #: One of :data:`SUBMISSION_PATHS` — must match the documented usage.
    submission: str
    #: One of the success-criteria kinds above.
    criteria: str
    #: Optional helm charts that must be force-enabled for this example
    #: (threaded to CDK as ``helm_enabled_overrides``).
    helm_overrides: tuple[str, ...] = ()
    #: Optional infrastructure features that must be force-enabled
    #: (threaded to CDK as ``feature_enabled_overrides``).
    feature_overrides: tuple[str, ...] = ()
    #: Accelerator requirement: "" (none), "nvidia", "neuron", or "efa".
    accelerator: str = ""
    #: Named setup/teardown driver hooks (implemented in checks/examples.py):
    #: "keda-demo-queue".
    setup_driver: str = ""
    #: Deliberate, report-disclosed manifest mutations applied before
    #: submission, as (json-path-ish description, replacement) pairs.
    mutations: dict[str, str] = field(default_factory=dict)
    #: Per-example completion timeout. GPU examples get longer defaults to
    #: absorb node provisioning.
    timeout_seconds: int = 900
    #: Skip unless the account has usable capacity for this instance family
    #: (checked via service quotas before submission); empty = never skipped.
    capacity_quota_code: str = ""
    #: Human rationale for anything unusual above.
    notes: str = ""


#: Sentinel mutation value: remove the targeted entry instead of replacing it.
REMOVE_VALUE = "__REMOVE__"

_GATED_MODEL_MUTATION_NOTE = (
    "the manifest's default model is HuggingFace-gated; validation substitutes "
    "the ungated facebook/opt-125m so the endpoint can become ready without "
    "credentials — the serving path itself is exercised unchanged"
)

EXAMPLE_SPECS: dict[str, ExampleSpec] = {
    spec.name: spec
    for spec in (
        # --- plain batch jobs over documented CLI paths -------------------
        ExampleSpec("simple-job", SUBMIT_SQS, JOB_COMPLETES),
        ExampleSpec(
            "sqs-job-submission",
            SUBMIT_SQS,
            JOB_COMPLETES,
            accelerator="nvidia",
            timeout_seconds=1800,
            notes="two Jobs in one file; the GPU one waits for node provisioning",
        ),
        ExampleSpec("model-download-job", KUBECTL_APPLY, JOB_COMPLETES, timeout_seconds=1200),
        ExampleSpec("efs-output-job", SUBMIT_DIRECT, JOB_COMPLETES),
        ExampleSpec(
            "fsx-lustre-job",
            SUBMIT_DIRECT,
            JOB_COMPLETES,
            feature_overrides=("fsx_lustre",),
        ),
        ExampleSpec(
            "aurora-pgvector-job",
            SUBMIT_DIRECT,
            JOB_COMPLETES,
            feature_overrides=("aurora_pgvector",),
        ),
        ExampleSpec(
            "vector-store-search-job",
            SUBMIT_DIRECT,
            JOB_COMPLETES,
            feature_overrides=("vector_store",),
            setup_driver="vector-demo-corpus",
            notes=(
                "read-only search; the setup driver ingests the bundled demo "
                "corpus (gco vector ingest --demo --wait) so the >=1-hit "
                "self-assert has something to find"
            ),
        ),
        ExampleSpec(
            "mlflow-tracking-job",
            SUBMIT_DIRECT,
            JOB_COMPLETES,
            setup_driver="mlflow-ready",
            notes=(
                "tracking server ships with the default-on observability "
                "bundle (no override key needed); the setup driver waits for "
                "the mlflow Deployment before the client job submits"
            ),
        ),
        ExampleSpec(
            "valkey-cache-job",
            SUBMIT_DIRECT,
            JOB_COMPLETES,
            feature_overrides=("valkey",),
        ),
        ExampleSpec("cluster-shared-bucket-upload-job", SUBMIT_DIRECT, JOB_COMPLETES),
        ExampleSpec(
            "analytics-s3-upload-job",
            SUBMIT_DIRECT,
            JOB_COMPLETES,
            notes=(
                "validates the cluster-side half only; the Studio-notebook "
                "reader documented as a prerequisite is out of scope"
            ),
        ),
        ExampleSpec(
            "analytics-database-export-job",
            SUBMIT_DIRECT,
            JOB_COMPLETES,
            feature_overrides=("aurora_pgvector",),
            notes="with Aurora forced on, the full export path runs (not the no-op branch)",
        ),
        # --- GPU / accelerator jobs ---------------------------------------
        ExampleSpec(
            "gpu-job", SUBMIT_SQS, JOB_COMPLETES, accelerator="nvidia", timeout_seconds=1800
        ),
        ExampleSpec(
            "multi-gpu-training",
            KUBECTL_APPLY,
            JOB_COMPLETES,
            accelerator="nvidia",
            timeout_seconds=2400,
            notes="indexed Job + headless Service; completion requires all indexes",
        ),
        ExampleSpec(
            "efa-distributed-training",
            SUBMIT_DIRECT,
            JOB_COMPLETES,
            accelerator="efa",
            capacity_quota_code="L-417A185B",
            timeout_seconds=2400,
            notes=(
                "requires P-family EFA-capable capacity; skipped with evidence "
                "when the account's Running On-Demand P quota is zero"
            ),
        ),
        ExampleSpec(
            "inferentia-job",
            SUBMIT_API,
            JOB_COMPLETES,
            accelerator="neuron",
            capacity_quota_code="L-1945791B",
            timeout_seconds=1800,
        ),
        ExampleSpec(
            "trainium-job",
            SUBMIT_API,
            JOB_COMPLETES,
            accelerator="neuron",
            capacity_quota_code="L-2C3B7624",
            timeout_seconds=1800,
        ),
        # --- inference Deployment+Service pairs ---------------------------
        ExampleSpec(
            "inference-vllm",
            SUBMIT_DIRECT,
            DEPLOYMENT_AVAILABLE,
            accelerator="nvidia",
            mutations={
                "Deployment.env.MODEL": "facebook/opt-125m",
                "Deployment.env.MAX_MODEL_LEN": "2048",
            },
            timeout_seconds=2400,
            notes=_GATED_MODEL_MUTATION_NOTE,
        ),
        ExampleSpec(
            "inference-sglang",
            SUBMIT_DIRECT,
            DEPLOYMENT_AVAILABLE,
            accelerator="nvidia",
            timeout_seconds=2400,
            notes="default model (microsoft/Phi-3.5-mini-instruct) is ungated; runs verbatim",
        ),
        ExampleSpec(
            "inference-tgi",
            SUBMIT_DIRECT,
            DEPLOYMENT_AVAILABLE,
            accelerator="nvidia",
            mutations={
                "Deployment.env.MODEL_ID": "facebook/opt-125m",
                # AWQ requires an AWQ-quantized checkpoint; the substitute
                # model is served unquantized.
                "Deployment.env.QUANTIZE": REMOVE_VALUE,
            },
            timeout_seconds=2400,
            notes=_GATED_MODEL_MUTATION_NOTE,
        ),
        ExampleSpec(
            "inference-torchserve",
            SUBMIT_DIRECT,
            DEPLOYMENT_AVAILABLE,
            accelerator="nvidia",
            timeout_seconds=2400,
            notes="serves from an (empty) EFS model store; readiness with no models is the documented initial state",
        ),
        ExampleSpec(
            "inference-triton",
            SUBMIT_DIRECT,
            DEPLOYMENT_AVAILABLE,
            accelerator="nvidia",
            timeout_seconds=2400,
            notes="empty --model-repository is a valid, live initial state",
        ),
        # --- scheduler CRD examples (documented kubectl paths) ------------
        ExampleSpec(
            "kueue-job",
            KUBECTL_APPLY,
            JOB_COMPLETES,
            accelerator="nvidia",
            timeout_seconds=2400,
            notes=(
                "applies its own ResourceFlavors/ClusterQueue/LocalQueue plus a "
                "CPU and a GPU Job; both Jobs must complete and the queue "
                "objects are deleted afterwards"
            ),
        ),
        ExampleSpec(
            "volcano-gang-job",
            KUBECTL_APPLY,
            VCJOB_COMPLETES,
            timeout_seconds=1200,
        ),
        ExampleSpec(
            "yunikorn-job",
            KUBECTL_APPLY,
            JOB_COMPLETES,
            helm_overrides=("yunikorn",),
            accelerator="nvidia",
            timeout_seconds=2400,
            notes="three Jobs incl. one GPU and one gang-annotated; all must complete",
        ),
        ExampleSpec(
            "slurm-cluster-job",
            KUBECTL_APPLY,
            JOB_COMPLETES,
            helm_overrides=("slurm",),
            timeout_seconds=1200,
        ),
        ExampleSpec(
            "ray-cluster",
            KUBECTL_APPLY,
            RAYCLUSTER_READY,
            timeout_seconds=1200,
        ),
        ExampleSpec(
            "kubeflow-trainjob",
            SUBMIT_SQS,
            TRAINJOB_COMPLETES,
            setup_driver="trainer-runtime-ready",
            timeout_seconds=1800,
            notes=(
                "CPU-sized 2-node torchrun all-reduce; the trainer chart is "
                "on by default (no override key), the setup driver waits for "
                "the TrainJob CRD and the torch-distributed runtime, and the "
                "timeout absorbs the multi-GB pytorch image pull on both nodes"
            ),
        ),
        ExampleSpec(
            "keda-scaled-job",
            KUBECTL_APPLY,
            SCALEDJOB_SCALES,
            setup_driver="keda-demo-queue",
            timeout_seconds=1200,
            notes=(
                "creates a disposable demo queue, grants the KEDA operator "
                "read-only metric access via a queue policy (the documented "
                "prerequisite), substitutes the placeholder queueURL, and "
                "deletes the queue afterwards"
            ),
        ),
        # --- DAG pipeline --------------------------------------------------
        ExampleSpec("pipeline-dag", DAG_RUN, DAG_SUCCEEDS, timeout_seconds=1800),
        ExampleSpec(
            "dag-step-preprocess",
            COMPANION,
            NONE,
            notes="step manifest executed via pipeline-dag",
        ),
        ExampleSpec(
            "dag-step-train", COMPANION, NONE, notes="step manifest executed via pipeline-dag"
        ),
    )
}


def required_helm_overrides(names: list[str]) -> tuple[str, ...]:
    """Union of helm overrides needed by the selected examples (sorted)."""
    needed: set[str] = set()
    for name in names:
        needed.update(EXAMPLE_SPECS[name].helm_overrides)
    return tuple(sorted(needed))


def required_feature_overrides(names: list[str]) -> tuple[str, ...]:
    """Union of feature overrides needed by the selected examples (sorted)."""
    needed: set[str] = set()
    for name in names:
        needed.update(EXAMPLE_SPECS[name].feature_overrides)
    return tuple(sorted(needed))
