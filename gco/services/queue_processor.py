"""
Queue Processor Service for GCO (Global Capacity Orchestrator on AWS).

Polls the regional SQS job queue, reads Kubernetes manifests from messages,
validates them, and applies them to the cluster. Designed to run as a
short-lived pod managed by a KEDA ScaledJob that scales based on queue depth.

Each invocation processes a single SQS message (which may contain multiple
manifests). On success the message is deleted; on failure it returns to the
queue after the visibility timeout (5 min) and eventually lands in the DLQ
after 3 failed attempts.

Message format — the shared envelope in ``gco.job_envelope``, which
``gco jobs submit-sqs`` builds and this service reads:
    {
        "job_id": "abc123",
        "manifests": [<k8s manifest dicts>],
        "namespace": "gco-jobs",
        "priority": 0,
        "submitted_at": "2026-03-26T12:00:00+00:00"
    }

Configuration via environment variables:
    JOB_QUEUE_URL:           SQS queue URL to consume from (required)
    AWS_REGION:              AWS region (default: us-east-1)
    ALLOWED_NAMESPACES:      Comma-separated namespace allowlist
                             (default: gco-jobs)
    ALLOWED_KINDS:           Comma-separated resource-kind allowlist shared
                             with the REST manifest processor
    MAX_GPU_PER_MANIFEST:    Max GPUs summed across all containers
                             (regular + init + ephemeral)
    MAX_CPU_PER_MANIFEST:    Max CPU summed across all containers; any
                             Kubernetes quantity ("500m", "10", "0.5")
    MAX_MEMORY_PER_MANIFEST: Max memory summed across all containers; any
                             Kubernetes quantity ("32Gi", "256Mi", a bare
                             byte count)
                             The three caps default to
                             gco.resource_governance.DEFAULT_MANIFEST_RESOURCE_CAPS,
                             the REST processor's defaults.
    TRUSTED_REGISTRIES:      Comma-separated list of registry domains
                             (e.g. "nvcr.io,public.ecr.aws"). Empty/unset
                             uses the REST processor's secure defaults.
                             Keep in sync with
                             cdk.json::job_validation_policy.trusted_registries.
    TRUSTED_DOCKERHUB_ORGS:  Comma-separated list of Docker Hub org names
                             (e.g. "nvidia,pytorch"). Empty/unset uses the
                             REST processor's secure defaults. Keep in sync
                             with cdk.json::job_validation_policy.trusted_dockerhub_orgs.
    K8S_API_TIMEOUT:         Seconds each Kubernetes API request attempt may
                             take (default: 30, gco.services.k8s_client)
    YAML_MAX_DEPTH:          Maximum manifest nesting depth (default: 50),
                             the REST processor's variable and default

Security policy toggles (all default to true except ``BLOCK_RUN_AS_ROOT``
which defaults to false, matching job_validation_policy.manifest_security_policy
in cdk.json). Each one controls whether the corresponding pod/container
setting is rejected:

    BLOCK_PRIVILEGED:             Reject ``securityContext.privileged: true``
                                  on pod or container (default: true)
    BLOCK_PRIVILEGE_ESCALATION:   Reject containers with
                                  allowPrivilegeEscalation=true
                                  (default: true)
    BLOCK_HOST_NETWORK:           Block pods with hostNetwork=true
                                  (default: true)
    BLOCK_HOST_PID:               Block pods with hostPID=true
                                  (default: true)
    BLOCK_HOST_IPC:               Block pods with hostIPC=true
                                  (default: true)
    BLOCK_HOST_PATH:              Block volumes referencing hostPath
                                  (default: true)
    BLOCK_ADDED_CAPABILITIES:     Block containers that add Linux
                                  capabilities via securityContext.capabilities.add
                                  (default: true)
    BLOCK_RUN_AS_ROOT:            Reject runAsUser: 0 at pod or container
                                  level (default: false — many public
                                  images still run as root)

The admission variables (the allowlists, caps, toggles,
REQUIRE_ACCELERATOR_TOLERATION and YAML_MAX_DEPTH) are the REST manifest
processor's, read with its defaults into the policy that ``validate_manifest``
judges every manifest against with ``gco.job_admission.admit_manifest``, the
REST service's own admission. The REST service's VALIDATION_ENABLED is not
read: this consumer always validates.
"""

from __future__ import annotations

import json
import logging
import os
import sys
import time
from typing import Any

import boto3
from kubernetes import client, config, dynamic
from kubernetes.client.exceptions import ApiException
from kubernetes.dynamic.exceptions import NotFoundError, ResourceNotFoundError

from gco.job_admission import (
    JobValidationPolicy,
    admit_manifest,
    parse_cpu_millicores,
    parse_memory_bytes,
)
from gco.job_envelope import DEFAULT_JOB_NAMESPACE
from gco.manifest_security_policy import parse_boolean_environment
from gco.models import ResourceStatus
from gco.resource_governance import DEFAULT_MANIFEST_RESOURCE_CAPS
from gco.services.k8s_client import BoundedApiClient
from gco.services.manifest_processor import (
    ADDON_KIND_HINTS,
    DEFAULT_ALLOWED_KINDS,
    DEFAULT_TRUSTED_DOCKERHUB_ORGS,
    DEFAULT_TRUSTED_REGISTRIES,
    RuntimeClassPresence,
)
from gco.services.structured_logging import sanitize_log_value
from gco.services.template_store import JobStore

# <pyflowchart-code-diagram> BEGIN - auto-inserted, do not edit
# Generated at (UTC): 2026-11-21T20:42:53Z
# Generated from Git commit: 1d908f3fa649c16f69e32c2af36f5992f62d94d7
# Flowchart(s) generated from this file:
#   * ``validate_manifest`` -> ``diagrams/code_diagrams/gco/services/queue_processor.validate_manifest.html``
#     (PNG: ``diagrams/code_diagrams/gco/services/queue_processor.validate_manifest.png``)
# Regenerate with ``SOURCE_DATE_EPOCH=<unix-seconds> GCO_DIAGRAM_SOURCE_COMMIT=<40-char-sha> python diagrams/generate.py --code-only``.
# <pyflowchart-code-diagram> END


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [queue-processor] %(message)s",
)
log = logging.getLogger("queue-processor")


# --- Configuration from environment ---
# These are set by the KEDA ScaledJob manifest (post-helm-sqs-consumer.yaml)
# and populated from cdk.json queue_processor settings during CDK deploy.
QUEUE_URL = os.environ.get("JOB_QUEUE_URL", "")
REGION = os.environ.get("AWS_REGION", os.environ.get("AWS_DEFAULT_REGION", "us-east-1"))
# Jobs-table failure recording. Set by post-helm-sqs-consumer.yaml from the
# stack's {{JOBS_TABLE_NAME}} replacement; the companion DYNAMODB_REGION env
# var is consumed by JobStore itself. Empty (an older deployed ScaledJob
# template) disables recording and preserves the pre-recording behavior.
JOBS_TABLE_NAME = os.environ.get("JOBS_TABLE_NAME", "")
# The job queue's redrive policy dead-letters a message after
# maxReceiveCount failed receives (3 — see the JobQueue definition in
# gco/stacks/regional_stack.py). The receive that reaches this count is the
# message's final delivery, so it is the one attempt whose failure is
# recorded as a terminal FAILED job record. Env override exists for tests
# and for operators who retune the queue's redrive policy out-of-band.
FINAL_RECEIVE_COUNT = int(os.environ.get("QP_JOB_QUEUE_MAX_RECEIVE_COUNT", "3"))
# Whether a Nitro Sandbox RuntimeClass exists here (validate_manifest). The
# client is built per lookup because the Kubernetes configuration is loaded
# after import; like every client here it bounds each request
# (gco.services.k8s_client).
runtime_class_exists = RuntimeClassPresence(
    lambda name: client.NodeV1Api(BoundedApiClient()).read_runtime_class(name)
)


def _comma_separated(value: str) -> list[str]:
    """The items of a comma-separated variable, stripped, without empty ones."""
    return [item.strip() for item in value.split(",") if item.strip()]


def job_validation_policy_from_environment() -> JobValidationPolicy:
    """The admission policy this consumer enforces, read from its environment.

    The ScaledJob (post-helm-sqs-consumer.yaml) carries the REST manifest
    processor's admission variables, rendered from the same cdk.json
    ``job_validation_policy``, and this reads them with the defaults and
    fallbacks ``create_manifest_processor_from_env`` applies, so the result
    equals the REST service's ``job_validation_policy()``
    (tests/test_job_submission_validation.py::TestOnePolicy pins that):

    * The caps default to ``DEFAULT_MANIFEST_RESOURCE_CAPS`` and take any
      Kubernetes quantity (``MAX_CPU_PER_MANIFEST=10`` is ten cores).
    * An unset ``ALLOWED_NAMESPACES`` or ``ALLOWED_KINDS`` is the shipped
      allowlist; one set to an empty value allows nothing.
    * An unset or empty ``TRUSTED_REGISTRIES`` or ``TRUSTED_DOCKERHUB_ORGS`` is
      the shipped allowlist, so a wiring error cannot switch image-source
      validation off.
    * The ``BLOCK_*`` toggles default to true except ``BLOCK_RUN_AS_ROOT``,
      and ``REQUIRE_ACCELERATOR_TOLERATION`` to true.

    ``VALIDATION_ENABLED``, the REST service's switch for turning admission
    off, is not read: the ScaledJob does not carry it, and a queue consumer
    that applies whatever arrives must never run without admission.

    Raises:
        ValueError: when a toggle is not an explicit boolean, or a cap or the
            depth is not a number, so the pod fails at startup rather than
            admit under a policy nobody configured.
    """
    allowed_namespaces = os.environ.get("ALLOWED_NAMESPACES")
    allowed_kinds = os.environ.get("ALLOWED_KINDS")
    trusted_registries = _comma_separated(os.environ.get("TRUSTED_REGISTRIES", ""))
    trusted_dockerhub_orgs = _comma_separated(os.environ.get("TRUSTED_DOCKERHUB_ORGS", ""))
    return JobValidationPolicy(
        max_cpu_millicores=parse_cpu_millicores(
            os.environ.get(
                "MAX_CPU_PER_MANIFEST",
                str(DEFAULT_MANIFEST_RESOURCE_CAPS["max_cpu_per_manifest"]),
            )
        ),
        max_memory_bytes=parse_memory_bytes(
            os.environ.get(
                "MAX_MEMORY_PER_MANIFEST",
                str(DEFAULT_MANIFEST_RESOURCE_CAPS["max_memory_per_manifest"]),
            )
        ),
        max_gpu_count=int(
            os.environ.get(
                "MAX_GPU_PER_MANIFEST",
                str(DEFAULT_MANIFEST_RESOURCE_CAPS["max_gpu_per_manifest"]),
            )
        ),
        allowed_namespaces=frozenset(
            ["gco-jobs"] if allowed_namespaces is None else _comma_separated(allowed_namespaces)
        ),
        allowed_kinds=frozenset(
            DEFAULT_ALLOWED_KINDS if allowed_kinds is None else _comma_separated(allowed_kinds)
        ),
        # Sorted, as ManifestProcessor.job_validation_policy() sorts them.
        trusted_registries=tuple(sorted(trusted_registries or DEFAULT_TRUSTED_REGISTRIES)),
        trusted_dockerhub_orgs=tuple(
            sorted(trusted_dockerhub_orgs or DEFAULT_TRUSTED_DOCKERHUB_ORGS)
        ),
        require_accelerator_toleration=parse_boolean_environment(
            "REQUIRE_ACCELERATOR_TOLERATION", True
        ),
        security={
            "block_privileged": parse_boolean_environment("BLOCK_PRIVILEGED", True),
            "block_privilege_escalation": parse_boolean_environment(
                "BLOCK_PRIVILEGE_ESCALATION", True
            ),
            "block_host_network": parse_boolean_environment("BLOCK_HOST_NETWORK", True),
            "block_host_pid": parse_boolean_environment("BLOCK_HOST_PID", True),
            "block_host_ipc": parse_boolean_environment("BLOCK_HOST_IPC", True),
            "block_host_path": parse_boolean_environment("BLOCK_HOST_PATH", True),
            "block_added_capabilities": parse_boolean_environment("BLOCK_ADDED_CAPABILITIES", True),
            "block_run_as_root": parse_boolean_environment("BLOCK_RUN_AS_ROOT", False),
        },
        validation_enabled=True,
        yaml_max_depth=int(os.environ.get("YAML_MAX_DEPTH", "50")),
    )


# The policy validate_manifest judges every manifest against. Read once, at
# import, so a malformed variable stops the pod before it receives a message.
JOB_VALIDATION_POLICY = job_validation_policy_from_environment()


# The standard in-cluster credential paths the kubernetes client reads. The
# worker's ServiceAccount sets ``automountServiceAccountToken: false``, so
# these exist ONLY because post-helm-sqs-consumer.yaml projects the
# ``kubernetes-api-token`` volume at this exact mount point.
_SERVICEACCOUNT_DIR = "/var/run/secrets/kubernetes.io/serviceaccount"
_SERVICEACCOUNT_TOKEN_PATH = f"{_SERVICEACCOUNT_DIR}/token"

# Mirrors gco.services.central_queue_worker._MAX_ERROR_LENGTH — every error
# string persisted to the jobs table is bounded the same way on both paths.
_MAX_ERROR_LENGTH = 2_000


def _bounded_error(value: object) -> str:
    """Bound user/runtime error text before persisting it in DynamoDB."""
    text = str(value)
    return text if len(text) <= _MAX_ERROR_LENGTH else f"{text[:_MAX_ERROR_LENGTH]}...[truncated]"


class KubernetesConfigurationError(RuntimeError):
    """The process cannot construct a Kubernetes client at all.

    Distinguishes a config/environment failure (the pod can never apply
    anything; retrying the same message cannot succeed) from a per-message
    failure (malformed body, validation, apply errors — which retain the
    message for visibility-timeout retry and the DLQ).
    """


def _incluster_failure_detail(error: Exception) -> str:
    """Name the actual broken precondition instead of the client's generic error."""
    service_account = (
        os.environ.get("SERVICE_ACCOUNT_NAME") or "<unknown - SERVICE_ACCOUNT_NAME unset>"
    )
    if not os.path.exists(_SERVICEACCOUNT_TOKEN_PATH):
        return (
            f"no Kubernetes API token at {_SERVICEACCOUNT_TOKEN_PATH}. This pod runs as "
            f"ServiceAccount {service_account!r}, which sets automountServiceAccountToken: "
            "false, so the token exists only when the workload projects one at "
            f"{_SERVICEACCOUNT_DIR} (the kubernetes-api-token projected volume in "
            "post-helm-sqs-consumer.yaml). Redeploy so the base-phase ServiceAccount "
            "hardening and the post-Helm token projection are from the same release."
        )
    return (
        f"a Kubernetes API token exists at {_SERVICEACCOUNT_TOKEN_PATH} but in-cluster "
        f"configuration still failed for ServiceAccount {service_account!r}: {error}"
    )


def load_k8s() -> None:
    """Load Kubernetes configuration (in-cluster, or local kubeconfig off-cluster).

    Inside a pod (``KUBERNETES_SERVICE_HOST`` set) an in-cluster failure is
    terminal and raises :class:`KubernetesConfigurationError` naming the
    missing token path and ServiceAccount — it must NOT fall through to
    ``load_kube_config()``, whose unguarded ``ConfigException`` used to kill
    the pod before its first SQS receive and crash-loop invisibly.
    """
    try:
        config.load_incluster_config()
        log.info("Loaded in-cluster Kubernetes configuration")
        return
    except config.ConfigException as incluster_error:
        if os.environ.get("KUBERNETES_SERVICE_HOST"):
            raise KubernetesConfigurationError(
                _incluster_failure_detail(incluster_error)
            ) from incluster_error
    try:
        config.load_kube_config()
        log.info("Loaded local kubeconfig")
    except config.ConfigException as kubeconfig_error:
        raise KubernetesConfigurationError(
            "no Kubernetes credentials available: not running in a cluster "
            f"(KUBERNETES_SERVICE_HOST unset) and no local kubeconfig: {kubeconfig_error}"
        ) from kubeconfig_error


def validate_manifest(m: dict[str, Any]) -> tuple[bool, str | None]:
    """Judge a manifest before it is applied, exactly as the REST service would.

    Returns :func:`gco.job_admission.admit_manifest`'s verdict, the one
    ``ManifestProcessor.validate_manifest`` returns, for
    :data:`JOB_VALIDATION_POLICY` and this cluster's sandbox RuntimeClasses:
    the same checks in the same order, worded the same way, so a submitter
    holding ``sqs:SendMessage`` on the job queue meets the REST path's
    admission and nothing weaker. A manifest without ``metadata.namespace`` is
    judged in ``gco-jobs``, where :func:`apply_manifest` creates it.

    Returns:
        ``(True, None)`` when the manifest is admitted, else ``(False, reason)``.
    """
    return admit_manifest(m, JOB_VALIDATION_POLICY, runtime_class_exists=runtime_class_exists)


def _extract_pod_spec(manifest: dict[str, Any]) -> dict[str, Any] | None:
    """Return the pod spec for any supported workload kind, or None.

    Mirrors manifest_processor._extract_pod_spec so the SQS path and the
    REST path apply the same injection semantics.
    """
    spec = manifest.get("spec")
    if not isinstance(spec, dict):
        return None

    kind = manifest.get("kind", "")

    # CronJob: spec.jobTemplate.spec.template.spec
    if kind == "CronJob":
        job_template = spec.get("jobTemplate")
        if isinstance(job_template, dict):
            job_spec = job_template.get("spec")
            if isinstance(job_spec, dict):
                template = job_spec.get("template")
                if isinstance(template, dict):
                    pod_spec = template.get("spec")
                    if isinstance(pod_spec, dict):
                        return pod_spec
        return None

    # Deployment / StatefulSet / DaemonSet / ReplicaSet / Job: spec.template.spec
    if "template" in spec:
        template = spec.get("template")
        if isinstance(template, dict):
            pod_spec = template.get("spec")
            if isinstance(pod_spec, dict):
                return pod_spec
        return None

    # Bare Pod: spec contains "containers" directly
    if "containers" in spec:
        return spec

    return None


def _inject_security_defaults(manifest: dict[str, Any]) -> dict[str, Any]:
    """Inject security defaults into a user-submitted manifest in-place.

    Currently sets ``automountServiceAccountToken: false`` on the pod spec
    unless the user has explicitly set it either way (uses setdefault).

    Mirrors manifest_processor._inject_security_defaults so jobs submitted
    via SQS get the same SA-token-theft protection as those submitted via
    the REST API.

    A TrainJob is left as it is: its pod template comes from the runtime, and
    a runtime patch's pod spec has no ``automountServiceAccountToken`` (Kubeflow
    Trainer 2.3.0's TrainJob CRD), so the API server would drop an injected
    one. The shipped ``torch-distributed`` ClusterTrainingRuntime sets it to
    false (docs/DISTRIBUTED_TRAINING.md, Runtimes).
    """
    if manifest.get("kind") == "TrainJob":
        return manifest
    pod_spec = _extract_pod_spec(manifest)
    if pod_spec is not None:
        pod_spec.setdefault("automountServiceAccountToken", False)
    return manifest


def _is_job_finished(job_resource: dict[str, Any]) -> bool:
    """Return whether a Kubernetes Job has a true terminal condition."""
    status = job_resource.get("status", {})
    conditions = status.get("conditions") or [] if isinstance(status, dict) else []
    return any(
        isinstance(condition, dict)
        and condition.get("type") in ("Complete", "Failed")
        and condition.get("status") == "True"
        for condition in conditions
    )


def apply_manifest(m: dict[str, Any]) -> ResourceStatus:
    """Apply one prevalidated manifest and return an explicit operation status.

    Unsupported API resources are failures, never successful skips. This keeps
    the owning SQS message available for retry and eventual DLQ inspection.
    """
    # Inject security defaults BEFORE applying so user pods never
    # auto-mount the default SA token (T-022 / M-113 parity with the
    # REST manifest_processor path).
    _inject_security_defaults(m)

    api_version = m["apiVersion"]
    kind = m["kind"]
    name = m["metadata"]["name"]
    namespace = m["metadata"].get("namespace", "gco-jobs")

    def status(result: str, message: str) -> ResourceStatus:
        return ResourceStatus(
            api_version=api_version,
            kind=kind,
            name=name,
            namespace=namespace,
            status=result,
            message=message,
        )

    dyn = dynamic.DynamicClient(BoundedApiClient())
    try:
        resource = dyn.resources.get(api_version=api_version, kind=kind)
    except ResourceNotFoundError:
        # A policy-allowed kind whose addon is not installed gets the
        # actionable remedy appended; the stable prefix is kept for DLQ
        # triage tooling.
        addon_hint = ADDON_KIND_HINTS.get(kind)
        detail = f" ({addon_hint})" if addon_hint else ""
        return status("failed", f"Unsupported Kubernetes resource {api_version}/{kind}{detail}")

    # For Jobs, delete completed/failed ones first so re-submission works.
    # Without this, re-submitting the same job name would fail with a 409 conflict
    # because Kubernetes doesn't allow creating a Job with the same name as an
    # existing one (even if it's finished).
    if kind == "Job":
        try:
            existing = resource.get(name=name, namespace=namespace)
            if _is_job_finished(existing):
                log.info("Deleting finished Job %s/%s before re-creation", namespace, name)
                resource.delete(
                    name=name,
                    namespace=namespace,
                    body=client.V1DeleteOptions(propagation_policy="Background"),
                )
                time.sleep(2)
        except (NotFoundError, ApiException) as e:
            log.debug("Pre-create lookup for Job %s/%s failed: %s", namespace, name, e)

    # Create-or-update pattern: try create first, fall back to patch on 409 (conflict).
    # This is idempotent — safe to retry without side effects.
    try:
        if resource.namespaced:
            resource.create(body=m, namespace=namespace)
        else:
            resource.create(body=m)
        return status("created", "Resource created successfully")
    except ApiException as e:
        if e.status == 409:
            try:
                if resource.namespaced:
                    resource.patch(body=m, name=name, namespace=namespace)
                else:
                    resource.patch(body=m, name=name)
                return status("updated", "Resource updated successfully")
            except ApiException as patch_err:
                return status("failed", f"Patch failed: {patch_err.reason}")
        return status("failed", f"Create failed: {e.reason}")
    except Exception as e:
        return status("failed", f"Unexpected apply error: {e}")


def process_one_message() -> bool:
    """Receive one SQS message and delete it only after complete success.

    ``True`` means either the poll was empty or the received message was fully
    validated, applied, and deleted. ``False`` means the message was not
    acknowledged (or queue configuration was invalid). Malformed, empty,
    invalid, unsupported, and apply-failed messages deliberately remain in SQS
    for visibility-timeout retries and eventual dead-letter-queue handling.
    """
    if not QUEUE_URL:
        log.error("JOB_QUEUE_URL not set")
        return False

    sqs = boto3.client("sqs", region_name=REGION)

    resp = sqs.receive_message(
        QueueUrl=QUEUE_URL,
        MaxNumberOfMessages=1,
        WaitTimeSeconds=5,
        MessageAttributeNames=["All"],
        AttributeNames=["ApproximateReceiveCount"],
    )

    messages = resp.get("Messages", [])
    if not messages:
        log.info("No messages in queue")
        return True

    msg = messages[0]
    receipt = msg.get("ReceiptHandle")
    if not receipt:
        log.error("Received SQS message without a receipt handle; cannot acknowledge it")
        return False

    try:
        body = json.loads(msg.get("Body", ""))
    except (json.JSONDecodeError, TypeError, RecursionError) as e:
        # RecursionError: one SQS message has room for more nesting than the
        # parser can follow (it gives up near 120k levels, 240 KB).
        log.error("Malformed SQS message body; retaining for retry/DLQ: %s", e)
        return False

    if not isinstance(body, dict):
        log.error("SQS message body must be a JSON object; retaining for retry/DLQ")
        return False

    job_id = body.get("job_id", "unknown")
    manifests = body.get("manifests")
    if not isinstance(manifests, list) or not manifests:
        log.error(
            "Job %s must contain a non-empty manifests list; retaining for retry/DLQ",
            job_id,
        )
        return False
    if any(not isinstance(manifest, dict) for manifest in manifests):
        log.error("Job %s contains a non-object manifest; retaining for retry/DLQ", job_id)
        return False

    log.info("Processing job_id=%s, manifests=%d", job_id, len(manifests))

    # Validate the entire batch before applying anything. A disallowed resource
    # later in the message must not leave an earlier resource partially applied.
    validation_errors: list[tuple[int, str | None]] = []
    for i, manifest in enumerate(manifests):
        try:
            ok, reason = validate_manifest(manifest)
        except Exception as e:
            ok, reason = False, f"validation error: {e}"
        if not ok:
            validation_errors.append((i, reason))
    if validation_errors:
        for i, reason in validation_errors:
            log.error("  manifest[%d] validation failed: %s", i, reason)
        log.error("Job %s failed prevalidation; message will return to queue", job_id)
        _record_failure_on_final_receive(
            msg,
            body,
            job_id,
            message="SQS job failed prevalidation",
            error="; ".join(f"manifest[{i}]: {reason}" for i, reason in validation_errors),
        )
        return False

    failed = False
    failure_details: list[str] = []
    for i, manifest in enumerate(manifests):
        try:
            result = apply_manifest(manifest)
        except Exception as e:
            log.error("  manifest[%d] apply raised: %s", i, e)
            failed = True
            failure_details.append(f"manifest[{i}] apply raised: {e}")
            continue
        log.info(
            "  manifest[%d]: %s %s/%s: %s",
            i,
            result.status,
            result.kind,
            result.name,
            result.message or "",
        )
        if not result.is_successful():
            failed = True
            failure_details.append(
                f"manifest[{i}] {result.kind}/{result.name}: {result.message or result.status}"
            )

    if failed:
        # Don't delete the SQS message — it will become visible again after the
        # visibility timeout and retry. The queue redrive policy eventually
        # moves it to the DLQ for operator inspection.
        log.error("Job %s had failures; message will return to queue", job_id)
        _record_failure_on_final_receive(
            msg,
            body,
            job_id,
            message="SQS job could not be applied to Kubernetes",
            error="; ".join(failure_details),
        )
        return False

    sqs.delete_message(QueueUrl=QUEUE_URL, ReceiptHandle=receipt)
    log.info("Job %s processed successfully", job_id)
    return True


def _receive_count(msg: dict[str, Any]) -> int:
    """The message's ApproximateReceiveCount, defaulting to 1 when unreadable."""
    raw = (msg.get("Attributes") or {}).get("ApproximateReceiveCount", "1")
    try:
        return max(int(raw), 1)
    except TypeError, ValueError:
        return 1


def _record_job_failure(
    job_id: str,
    *,
    namespace: str,
    error: str,
    message: str,
    priority: int = 0,
    submitted_at: str | None = None,
) -> bool:
    """Best-effort terminal FAILED record for an SQS job; never raises.

    Recording failures must not change SQS retention semantics — the queue
    and its DLQ remain the source of truth when DynamoDB is unreachable, so
    every failure here is swallowed after logging.
    """
    if not JOBS_TABLE_NAME:
        log.error(
            "JOBS_TABLE_NAME not set; job %s failure will not be recorded",
            sanitize_log_value(job_id),
        )
        return False
    try:
        store = JobStore(table_name=JOBS_TABLE_NAME)
        created = store.record_job_failure(
            job_id,
            target_region=REGION,
            namespace=namespace,
            error=_bounded_error(error),
            message=message,
            priority=priority,
            submitted_at=submitted_at,
        )
    except Exception:
        log.exception("Unable to record FAILED for job %s", sanitize_log_value(job_id))
        return False
    if created:
        log.info("Recorded FAILED job record for %s", sanitize_log_value(job_id))
    else:
        log.info(
            "Job %s already has a centralized queue record; leaving it untouched",
            sanitize_log_value(job_id),
        )
    return created


def _record_failure_on_final_receive(
    msg: dict[str, Any],
    body: dict[str, Any],
    job_id: str,
    *,
    message: str,
    error: str,
) -> None:
    """Persist a FAILED record when this receive is the message's last delivery.

    Earlier receives keep today's retain-for-retry behavior untouched — a
    transient failure that succeeds on retry must not leave a terminal
    record. On the final delivery (the receive that exhausts the queue's
    redrive maxReceiveCount) the message is about to dead-letter, so its
    job is recorded FAILED with the bounded failure detail.
    """
    if _receive_count(msg) < FINAL_RECEIVE_COUNT:
        return
    priority = body.get("priority", 0)
    if not isinstance(priority, int):
        priority = 0
    submitted_at = body.get("submitted_at")
    _record_job_failure(
        job_id,
        namespace=str(body.get("namespace", DEFAULT_JOB_NAMESPACE)),
        error=error,
        message=message,
        priority=priority,
        submitted_at=submitted_at if isinstance(submitted_at, str) else None,
    )


def drain_one_message_after_config_failure(reason: str) -> bool:
    """Convert one queued message into a visible FAILED record, then delete it.

    A pod that cannot construct a Kubernetes client can never apply
    anything, and the failure happens before the first SQS receive — so
    the receive count never increments, the redrive policy never fires,
    and KEDA restarts the loop forever with nothing recorded. That is the
    exact shape of the SQS submission-path outage this module is being
    hardened against. Draining one message per failed pod records the
    terminal failure where operators and submitters look (the jobs table)
    while emptying the queue at the same bounded rate KEDA scales pods.

    The message is deleted ONLY after its ``job_id`` was recorded FAILED.
    A message whose ``job_id`` cannot be parsed — or whose record could not
    be written — is retained for the normal visibility-timeout/DLQ path.
    Returns whether one message was drained with a record.
    """
    if not QUEUE_URL:
        log.error("JOB_QUEUE_URL not set")
        return False
    if not JOBS_TABLE_NAME:
        log.error("JOBS_TABLE_NAME not set; cannot record failures, leaving the queue untouched")
        return False

    sqs = boto3.client("sqs", region_name=REGION)
    resp = sqs.receive_message(
        QueueUrl=QUEUE_URL,
        MaxNumberOfMessages=1,
        WaitTimeSeconds=5,
        MessageAttributeNames=["All"],
        AttributeNames=["ApproximateReceiveCount"],
    )
    messages = resp.get("Messages", [])
    if not messages:
        log.info("No messages in queue to drain")
        return False

    msg = messages[0]
    receipt = msg.get("ReceiptHandle")
    try:
        body = json.loads(msg.get("Body", ""))
    except json.JSONDecodeError, TypeError:
        body = None
    job_id = body.get("job_id") if isinstance(body, dict) else None
    if not receipt or not isinstance(job_id, str) or not job_id:
        log.error("Cannot identify the job in the queued message; retaining it for the DLQ")
        return False

    priority = body.get("priority", 0)
    submitted_at = body.get("submitted_at")
    recorded = _record_job_failure(
        job_id,
        namespace=str(body.get("namespace", DEFAULT_JOB_NAMESPACE)),
        error=f"queue processor could not initialize Kubernetes credentials: {reason}",
        message="Queue processor configuration failure",
        priority=priority if isinstance(priority, int) else 0,
        submitted_at=submitted_at if isinstance(submitted_at, str) else None,
    )
    if not recorded:
        return False

    sqs.delete_message(QueueUrl=QUEUE_URL, ReceiptHandle=receipt)
    log.error(
        "Job %s recorded FAILED and drained after a configuration failure",
        sanitize_log_value(job_id),
    )
    return True


def main() -> None:
    """Entry point for the queue processor.

    Configuration failures are terminal and loud: the error is emitted in a
    single structured line, one queued message is drained into a FAILED job
    record so the queue depth and job status both move, and the process
    exits nonzero. Per-message failures keep their retain-for-retry
    semantics via :func:`process_one_message`.
    """
    try:
        load_k8s()
    except KubernetesConfigurationError as error:
        drained = drain_one_message_after_config_failure(str(error))
        log.error(
            "terminal=config-failure drained_with_record=%s detail=%s",
            drained,
            error,
        )
        sys.exit(1)
    success = process_one_message()
    if not success:
        sys.exit(1)


if __name__ == "__main__":
    main()
