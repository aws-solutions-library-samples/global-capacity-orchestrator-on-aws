"""
Queue Processor Service for GCO (Global Capacity Orchestrator on AWS).

Polls the regional SQS job queue, reads Kubernetes manifests from messages,
validates them, and applies them to the cluster. Designed to run as a
short-lived pod managed by a KEDA ScaledJob that scales based on queue depth.

Each invocation processes a single SQS message (which may contain multiple
manifests). On success the message is deleted; on failure it returns to the
queue after the visibility timeout (5 min) and eventually lands in the DLQ
after 3 failed attempts.

Message format (produced by `gco jobs submit-sqs`):
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
                             (default: default,gco-jobs)
    MAX_GPU_PER_MANIFEST:    Max GPUs summed across all containers
                             (regular + init + ephemeral) (default: 4)
    MAX_CPU_PER_MANIFEST:    Max CPU summed across all containers; accepts
                             K8s suffixes ("500m" or "10" for cores)
                             (default: 10000 millicores = 10 cores)
    MAX_MEMORY_PER_MANIFEST: Max memory summed across all containers;
                             accepts K8s suffixes ("32Gi", "256Mi") or
                             a bare byte count (default: 32Gi)
    TRUSTED_REGISTRIES:      Comma-separated list of registry domains
                             (e.g. "nvcr.io,public.ecr.aws"). Empty/unset
                             disables the image registry check (fail-open).
                             Keep in sync with
                             cdk.json::job_validation_policy.trusted_registries.
    TRUSTED_DOCKERHUB_ORGS:  Comma-separated list of Docker Hub org names
                             (e.g. "nvidia,pytorch"). Empty/unset disables
                             the check. Keep in sync with
                             cdk.json::job_validation_policy.trusted_dockerhub_orgs.

Security policy toggles (all default to true except ``BLOCK_RUN_AS_ROOT``
which defaults to false, matching job_validation_policy.manifest_security_policy
in cdk.json). Each one controls whether the corresponding pod/container
setting is rejected; the REST manifest_processor enforces an identical set
so both submission paths apply the same policy:

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
from kubernetes.client.rest import ApiException
from kubernetes.dynamic.exceptions import NotFoundError, ResourceNotFoundError

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [queue-processor] %(message)s",
)
log = logging.getLogger("queue-processor")


def _parse_cpu_string(cpu_str: str) -> int:
    """Parse a Kubernetes-style CPU string to millicores.

    Accepts:
      - Millicore suffix: "500m" -> 500
      - Whole cores: "4" -> 4000
      - Bare millicore counts: "10000" (when > 999) stays as millicores
    """
    if not cpu_str:
        return 0
    s = cpu_str.strip()
    if s.endswith("m"):
        return int(s[:-1])
    return int(s) * 1000


def _parse_memory_string(memory_str: str) -> int:
    """Parse a Kubernetes-style memory string to bytes.

    Accepts binary suffixes (Ki, Mi, Gi, Ti), decimal suffixes (k, M, G),
    or a bare byte count.
    """
    if not memory_str:
        return 0
    s = memory_str.strip()
    if s.endswith("Ki"):
        return int(s[:-2]) * 1024
    if s.endswith("Mi"):
        return int(s[:-2]) * 1024**2
    if s.endswith("Gi"):
        return int(s[:-2]) * 1024**3
    if s.endswith("Ti"):
        return int(s[:-2]) * 1024**4
    if s.endswith("k"):
        return int(s[:-1]) * 1000
    if s.endswith("M"):
        return int(s[:-1]) * 1000**2
    if s.endswith("G"):
        return int(s[:-1]) * 1000**3
    return int(s)


# --- Configuration from environment ---
# These are set by the KEDA ScaledJob manifest (post-helm-sqs-consumer.yaml)
# and populated from cdk.json queue_processor settings during CDK deploy.
QUEUE_URL = os.environ.get("JOB_QUEUE_URL", "")
REGION = os.environ.get("AWS_REGION", os.environ.get("AWS_DEFAULT_REGION", "us-east-1"))
ALLOWED_NAMESPACES = set(os.environ.get("ALLOWED_NAMESPACES", "default,gco-jobs").split(","))
MAX_CPU = _parse_cpu_string(os.environ.get("MAX_CPU_PER_MANIFEST", "10000"))  # millicores
MAX_MEMORY = _parse_memory_string(os.environ.get("MAX_MEMORY_PER_MANIFEST", "32Gi"))  # bytes
MAX_GPU = int(os.environ.get("MAX_GPU_PER_MANIFEST", "4"))

# Accelerator resource keys and their node taint keys (taint key == resource
# key for all three). Kept in sync with the mirror in
# gco/services/manifest_processor.py::ACCELERATOR_TAINTS.
ACCELERATOR_TAINTS = ("nvidia.com/gpu", "aws.amazon.com/neuron", "vpc.amazonaws.com/efa")

# Trusted image sources (populated from cdk.json::manifest_processor at deploy time).
# Comma-separated env vars; empty/unset disables the check (fail-open logged).
# Keep in sync with gco/services/manifest_processor.py::_validate_image_sources.
TRUSTED_REGISTRIES = [
    r.strip() for r in os.environ.get("TRUSTED_REGISTRIES", "").split(",") if r.strip()
]
TRUSTED_DOCKERHUB_ORGS = [
    o.strip() for o in os.environ.get("TRUSTED_DOCKERHUB_ORGS", "").split(",") if o.strip()
]


def _env_bool(name: str, default: bool) -> bool:
    """Parse a boolean environment variable.

    Empty/unset returns ``default``. Recognized truthy values: "true", "1",
    "yes", "on" (case-insensitive). Everything else is falsy.
    """
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    return raw.strip().lower() in ("true", "1", "yes", "on")


# Security-policy toggles. Every one of these mirrors an attribute the REST
# manifest_processor exposes via cdk.json::job_validation_policy.manifest_security_policy.
# Both submission paths MUST enforce the same policy — an attacker holding
# sqs:SendMessage on the job queue must not be able to bypass checks the REST
# path applies. Structural parity is pinned by
# tests/test_queue_processor.py::TestSecurityPolicyParityWithManifestProcessor.
BLOCK_PRIVILEGED = _env_bool("BLOCK_PRIVILEGED", True)
BLOCK_PRIVILEGE_ESCALATION = _env_bool("BLOCK_PRIVILEGE_ESCALATION", True)
BLOCK_HOST_NETWORK = _env_bool("BLOCK_HOST_NETWORK", True)
BLOCK_HOST_PID = _env_bool("BLOCK_HOST_PID", True)
BLOCK_HOST_IPC = _env_bool("BLOCK_HOST_IPC", True)
BLOCK_HOST_PATH = _env_bool("BLOCK_HOST_PATH", True)
BLOCK_ADDED_CAPABILITIES = _env_bool("BLOCK_ADDED_CAPABILITIES", True)
BLOCK_RUN_AS_ROOT = _env_bool("BLOCK_RUN_AS_ROOT", False)

# Hard-reject accelerator jobs that lack a matching node toleration. Mirrors
# manifest_processor.require_accelerator_toleration so the SQS path is not a
# bypass.
REQUIRE_ACCELERATOR_TOLERATION = _env_bool("REQUIRE_ACCELERATOR_TOLERATION", True)


def _is_registry_domain(entry: str) -> bool:
    """True if the entry looks like a registry domain (has '.' or ':')."""
    return "." in entry or ":" in entry


def _positive_quantity(value: Any) -> bool:
    """True if a K8s resource quantity is present and greater than zero."""
    if value is None:
        return False
    try:
        return float(value) > 0
    except TypeError, ValueError:
        return True


def _toleration_matches(tolerations: list[dict[str, Any]], taint_key: str) -> bool:
    """True if *tolerations* tolerates the ``<taint_key>=true:NoSchedule`` taint.

    Matches manifest_processor._toleration_matches: the toleration's ``key``
    must equal *taint_key*, its effect must be empty or ``NoSchedule``, and it
    must use ``operator: Exists`` or ``operator: Equal`` with ``value: "true"``.
    """
    for tol in tolerations:
        if not isinstance(tol, dict) or tol.get("key") != taint_key:
            continue
        effect = tol.get("effect", "")
        if effect not in ("", "NoSchedule"):
            continue
        operator = tol.get("operator", "Equal")
        if operator == "Exists":
            return True
        if operator == "Equal" and str(tol.get("value")) == "true":
            return True
    return False


def _requested_accelerators(pod_spec: dict[str, Any]) -> set[str]:
    """Return the set of accelerator taint keys any container requests."""
    requested: set[str] = set()
    for _kind, c in _iter_containers(pod_spec):
        res = c.get("resources", {}) or {}
        for section in ("requests", "limits"):
            values = res.get(section, {}) or {}
            for taint in ACCELERATOR_TAINTS:
                if _positive_quantity(values.get(taint)):
                    requested.add(taint)
    return requested


def _iter_containers(pod_spec: dict[str, Any]) -> list[tuple[str, dict[str, Any]]]:
    """Yield (kind, container_dict) for every container, initContainer, and
    ephemeralContainer in a pod spec."""
    out: list[tuple[str, dict[str, Any]]] = []
    for c in pod_spec.get("containers", []) or []:
        out.append(("container", c))
    for c in pod_spec.get("initContainers", []) or []:
        out.append(("initContainer", c))
    for c in pod_spec.get("ephemeralContainers", []) or []:
        out.append(("ephemeralContainer", c))
    return out


def _is_image_trusted(image: str) -> bool:
    """True if the image reference is from a trusted registry or Docker Hub org.

    Matches the semantics of manifest_processor._validate_image_sources:
      1. Official Docker Hub images (no '/') are always allowed
      2. Images with a registry domain (first segment has '.' or ':') must
         match an entry in TRUSTED_REGISTRIES exactly (or a multi-segment
         prefix like "public.ecr.aws/lambda")
      3. Docker Hub images with an org (first segment has no '.' or ':') must
         match an entry in TRUSTED_DOCKERHUB_ORGS

    If both allowlists are empty the check is disabled (fail-open, logged).
    """
    if not TRUSTED_REGISTRIES and not TRUSTED_DOCKERHUB_ORGS:
        return True
    if not image:
        return True
    if "/" not in image:
        # Case 1: Official Docker Hub image — always trusted
        return True
    first = image.split("/", 1)[0]
    if _is_registry_domain(first):
        for registry in TRUSTED_REGISTRIES:
            if first == registry or image.startswith(registry + "/"):
                return True
        return False
    return first in TRUSTED_DOCKERHUB_ORGS


def load_k8s() -> None:
    """Load Kubernetes configuration (in-cluster or local kubeconfig)."""
    try:
        config.load_incluster_config()
        log.info("Loaded in-cluster Kubernetes configuration")
    except config.ConfigException:
        config.load_kube_config()
        log.info("Loaded local kubeconfig")


def validate_manifest(m: dict[str, Any]) -> tuple[bool, str]:
    """Validate a manifest before applying it to the cluster.

    The queue processor mirrors the security checks performed by the REST
    `manifest_processor` service (``gco/services/manifest_processor.py``)
    so that the SQS path cannot bypass them. Checks performed:

    1. **Namespace allowlist** — manifest namespace must be in
       ``ALLOWED_NAMESPACES`` (from ``ALLOWED_NAMESPACES`` env var,
       populated from ``cdk.json::job_validation_policy.allowed_namespaces``,
       shared with the REST manifest_processor).

    2. **Pod-level security policy** (configurable via cdk.json::
       job_validation_policy.manifest_security_policy, shared between both
       services). Rejects ``hostNetwork``, ``hostPID``, ``hostIPC``,
       ``hostPath`` volumes, privileged pod security context, and
       (if ``BLOCK_RUN_AS_ROOT``) pod-level ``runAsUser: 0``.

    3. **Container-level security policy** — for every container kind
       (regular, init, ephemeral) rejects ``privileged``,
       ``allowPrivilegeEscalation``, ``capabilities.add``, and (if
       ``BLOCK_RUN_AS_ROOT``) container-level ``runAsUser: 0``. Iterating
       every container kind catches the classic "smuggle it via an init
       container" bypass.

    4. **Image registry allowlist** — every container's image must come
       from ``TRUSTED_REGISTRIES`` (registry domains like ``nvcr.io``)
       or ``TRUSTED_DOCKERHUB_ORGS`` (Docker Hub orgs like ``nvidia``).
       Official Docker Hub images with no slash are always allowed. When
       both allowlists are empty the check is disabled. Keep the lists in
       sync with ``cdk.json::job_validation_policy.trusted_registries`` and
       ``trusted_dockerhub_orgs`` — CDK wires the same config into both
       services.

    5. **Resource caps** — the TOTAL CPU, memory, and GPU across ALL
       containers (regular + init + ephemeral) must not exceed
       ``MAX_CPU``, ``MAX_MEMORY``, and ``MAX_GPU``. This matches
       ``manifest_processor._validate_resource_limits`` — K8s accounts
       init/ephemeral resources differently at scheduling time, but
       from an enforcement perspective we sum them so an operator's
       ``max_*_per_manifest`` budget is a hard cap regardless of where
       the request is placed.

    Returns:
        ``(True, "")`` if the manifest is accepted, otherwise
        ``(False, reason)`` where ``reason`` is a human-readable string.
    """
    kind = m.get("kind")
    if not kind:
        return False, "missing 'kind'"
    api = m.get("apiVersion")
    if not api:
        return False, "missing 'apiVersion'"
    meta = m.get("metadata", {})
    if not meta.get("name"):
        return False, "missing 'metadata.name'"
    ns = meta.get("namespace", "default")
    if ns not in ALLOWED_NAMESPACES:
        return False, f"namespace '{ns}' not in allowed list {ALLOWED_NAMESPACES}"

    # Get pod spec for security and resource checks.
    # Handle multiple resource shapes, matching manifest_processor._get_all_containers:
    #   - Deployments / StatefulSets / ReplicaSets / DaemonSets / Jobs: spec.template.spec
    #   - CronJob: spec.jobTemplate.spec.template.spec
    #   - Pod (bare): spec (has 'containers' directly)
    spec = m.get("spec", {})
    pod_spec = None
    if "template" in spec:
        pod_spec = spec["template"].get("spec", {})
    elif "jobTemplate" in spec:
        pod_spec = spec["jobTemplate"].get("spec", {}).get("template", {}).get("spec", {})
    elif "containers" in spec:
        # Plain Pod manifest
        pod_spec = spec

    if pod_spec:
        all_containers = _iter_containers(pod_spec)

        # --- Accelerator toleration check ---
        # Mirror manifest_processor._validate_tolerations: a job requesting a
        # GPU/Neuron/EFA resource must carry a matching toleration or it would
        # stay Pending forever on tainted accelerator nodes.
        if REQUIRE_ACCELERATOR_TOLERATION:
            tolerations = pod_spec.get("tolerations", []) or []
            for taint in _requested_accelerators(pod_spec):
                if not _toleration_matches(tolerations, taint):
                    hint = (
                        f"add a matching toleration (e.g. key '{taint}', operator "
                        "'Exists', effect 'NoSchedule'); see examples/gpu-job.yaml"
                    )
                    return (
                        False,
                        f"Job requests accelerator '{taint}' but no matching "
                        f"toleration for taint {taint}=true:NoSchedule was found. {hint}",
                    )

        # --- Pod-level security policy checks ---
        # Mirror manifest_processor._validate_security_context so the SQS
        # path enforces the same policy as the REST path.
        if BLOCK_HOST_NETWORK and pod_spec.get("hostNetwork", False):
            return False, "hostNetwork is not permitted"
        if BLOCK_HOST_PID and pod_spec.get("hostPID", False):
            return False, "hostPID is not permitted"
        if BLOCK_HOST_IPC and pod_spec.get("hostIPC", False):
            return False, "hostIPC is not permitted"
        if BLOCK_HOST_PATH:
            for volume in pod_spec.get("volumes", []) or []:
                if volume.get("hostPath") is not None:
                    return False, "hostPath volumes are not permitted"

        pod_security_context = pod_spec.get("securityContext", {}) or {}
        if BLOCK_PRIVILEGED and pod_security_context.get("privileged", False):
            return False, "privileged pod security context is not permitted"
        if BLOCK_RUN_AS_ROOT:
            pod_run_as_user = pod_security_context.get("runAsUser")
            if pod_run_as_user is not None and pod_run_as_user == 0:
                return False, "running as root (runAsUser: 0) is not permitted"

        # --- Container-level security policy checks ---
        # Every toggle is applied to every container kind (regular, init,
        # ephemeral). An init container running as root or with CAP_SYS_ADMIN
        # has the same blast radius as a regular container running the same
        # way; there is no reason to give any kind a free pass.
        for kind, c in all_containers:
            cname = c.get("name", "unknown")
            sc = c.get("securityContext", {}) or {}
            if BLOCK_PRIVILEGED and sc.get("privileged", False):
                return False, f"{kind} '{cname}': privileged containers are not permitted"
            if BLOCK_PRIVILEGE_ESCALATION and sc.get("allowPrivilegeEscalation", False):
                return False, f"{kind} '{cname}': allowPrivilegeEscalation is not permitted"
            if BLOCK_ADDED_CAPABILITIES:
                added_caps = (sc.get("capabilities", {}) or {}).get("add", []) or []
                if added_caps:
                    return False, f"{kind} '{cname}': added capabilities are not permitted"
            if BLOCK_RUN_AS_ROOT:
                ras = sc.get("runAsUser")
                if ras is not None and ras == 0:
                    return (
                        False,
                        f"{kind} '{cname}': running as root (runAsUser: 0) is not permitted",
                    )

        # Enforce image registry allowlist (matches manifest_processor semantics)
        for kind, c in all_containers:
            image = c.get("image", "")
            if not _is_image_trusted(image):
                cname = c.get("name", "unknown")
                return (
                    False,
                    f"{kind} '{cname}': untrusted image source '{image}'",
                )

        # Enforce resource caps across ALL container kinds.
        # Sum the resource requests/limits of every container (regular,
        # init, and ephemeral). This is stricter than the K8s scheduler's
        # accounting but matches our security intent: an operator's
        # configured "max CPU/memory/GPU per manifest" is a hard cap on
        # the total resources a submitter can request regardless of
        # which container kind carries the request.
        total_gpu = 0
        total_cpu = 0
        total_memory = 0
        for _kind, c in all_containers:
            res = c.get("resources", {}) or {}
            limits = res.get("limits", {}) or {}
            requests = res.get("requests", {}) or {}
            gpu = limits.get("nvidia.com/gpu") or requests.get("nvidia.com/gpu", "0")  # nosec B113 - dict.get(), not HTTP requests
            total_gpu += int(gpu)
            cpu_str = limits.get("cpu") or requests.get("cpu", "0")  # nosec B113 - dict.get(), not HTTP requests
            if isinstance(cpu_str, str) and cpu_str.endswith("m"):
                total_cpu += int(cpu_str[:-1])
            else:
                total_cpu += int(float(cpu_str) * 1000)
            mem_str = limits.get("memory") or requests.get("memory", "0")  # nosec B113 - dict.get(), not HTTP requests
            if isinstance(mem_str, str):
                if mem_str.endswith("Gi"):
                    total_memory += int(float(mem_str[:-2]) * 1024**3)
                elif mem_str.endswith("Mi"):
                    total_memory += int(float(mem_str[:-2]) * 1024**2)
                elif mem_str.endswith("Ki"):
                    total_memory += int(float(mem_str[:-2]) * 1024)
                else:
                    total_memory += int(mem_str)
            else:
                total_memory += int(mem_str)

        errors = []
        if total_gpu > MAX_GPU:
            errors.append(f"GPU {total_gpu} exceeds max {MAX_GPU}")
        if total_cpu > MAX_CPU:
            errors.append(f"CPU {total_cpu}m exceeds max {MAX_CPU}m")
        if total_memory > MAX_MEMORY:
            errors.append(
                f"Memory {total_memory / (1024**3):.0f}Gi "
                f"exceeds max {MAX_MEMORY / (1024**3):.0f}Gi"
            )
        if errors:
            hint = (
                "To raise limits, update queue_processor in cdk.json "
                "and redeploy (see examples/README.md#troubleshooting)"
            )
            return False, "; ".join(errors) + f". {hint}"

    return True, ""


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
    """
    pod_spec = _extract_pod_spec(manifest)
    if pod_spec is not None:
        pod_spec.setdefault("automountServiceAccountToken", False)
    return manifest


def apply_manifest(m: dict[str, Any]) -> str:
    """Apply a single manifest using the dynamic Kubernetes client."""
    # Inject security defaults BEFORE applying so user pods never
    # auto-mount the default SA token (T-022 / M-113 parity with the
    # REST manifest_processor path).
    _inject_security_defaults(m)

    dyn = dynamic.DynamicClient(client.ApiClient())
    api_version = m["apiVersion"]
    kind = m["kind"]
    name = m["metadata"]["name"]
    namespace = m["metadata"].get("namespace", "default")

    try:
        resource = dyn.resources.get(api_version=api_version, kind=kind)
    except ResourceNotFoundError:
        return f"SKIP unknown resource {api_version}/{kind}"

    # For Jobs, delete completed/failed ones first so re-submission works.
    # Without this, re-submitting the same job name would fail with a 409 conflict
    # because Kubernetes doesn't allow creating a Job with the same name as an
    # existing one (even if it's finished).
    if kind == "Job":
        try:
            existing = resource.get(name=name, namespace=namespace)
            conditions = existing.get("status", {}).get("conditions", [])
            finished = any(c.get("type") in ("Complete", "Failed") for c in conditions)
            if finished:
                log.info(f"Deleting finished Job {namespace}/{name} before re-creation")
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
        return f"CREATED {kind}/{name}"
    except ApiException as e:
        if e.status == 409:
            try:
                if resource.namespaced:
                    resource.patch(body=m, name=name, namespace=namespace)
                else:
                    resource.patch(body=m, name=name)
                return f"UPDATED {kind}/{name}"
            except ApiException as patch_err:
                return f"PATCH_FAILED {kind}/{name}: {patch_err.reason}"
        return f"CREATE_FAILED {kind}/{name}: {e.reason}"


def process_one_message() -> bool:
    """Receive and process a single SQS message. Returns True on success."""
    if not QUEUE_URL:
        log.error("JOB_QUEUE_URL not set")
        return False

    sqs = boto3.client("sqs", region_name=REGION)

    resp = sqs.receive_message(
        QueueUrl=QUEUE_URL,
        MaxNumberOfMessages=1,
        WaitTimeSeconds=5,
        MessageAttributeNames=["All"],
    )

    messages = resp.get("Messages", [])
    if not messages:
        log.info("No messages in queue")
        return True

    msg = messages[0]
    receipt = msg["ReceiptHandle"]
    body = json.loads(msg["Body"])

    job_id = body.get("job_id", "unknown")
    manifests = body.get("manifests", [])
    log.info(f"Processing job_id={job_id}, manifests={len(manifests)}")

    results: list[str] = []
    failed = False
    for i, m in enumerate(manifests):
        ok, reason = validate_manifest(m)
        if not ok:
            log.error(f"  manifest[{i}] validation failed: {reason}")
            results.append(f"INVALID: {reason}")
            failed = True
            continue
        result = apply_manifest(m)
        log.info(f"  manifest[{i}]: {result}")
        results.append(result)
        if "FAILED" in result:
            failed = True

    if failed:
        # Don't delete the SQS message — it will become visible again after the
        # visibility timeout (5 min) and retry. After 3 total failures, SQS
        # moves it to the dead-letter queue for manual inspection.
        log.error(f"Job {job_id} had failures — message will return to queue")
        return False

    sqs.delete_message(QueueUrl=QUEUE_URL, ReceiptHandle=receipt)
    log.info(f"Job {job_id} processed successfully")
    return True


def main() -> None:
    """Entry point for the queue processor."""
    load_k8s()
    success = process_one_message()
    if not success:
        sys.exit(1)


if __name__ == "__main__":
    main()
