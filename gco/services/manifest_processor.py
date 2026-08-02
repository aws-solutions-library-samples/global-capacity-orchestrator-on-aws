"""
Manifest Processor Service for GCO (Global Capacity Orchestrator on AWS).

This service processes Kubernetes manifest submissions, validates them against
security and resource constraints, and applies them to the cluster.

Key Features:
- Validates manifests for required fields and structure
- Enforces namespace restrictions (only allowed namespaces)
- Enforces resource limits (CPU, memory, GPU per manifest)
- Validates security context (no privileged containers)
- Validates image sources (trusted registries only)
- Supports dry-run mode for validation without applying

Security Validations:
- Namespace must be in allowed list (default: gco-jobs)
- No privileged containers or privilege escalation
- Images must be from trusted registries
- Resource requests/limits within configured maximums

Environment Variables:
    CLUSTER_NAME: Name of the EKS cluster
    REGION: AWS region of the cluster
    MAX_CPU_PER_MANIFEST: Maximum CPU (millicores) per manifest (default: 10000)
    MAX_MEMORY_PER_MANIFEST: Maximum memory per manifest (default: 32Gi)
    MAX_GPU_PER_MANIFEST: Maximum GPUs per manifest (default: 4)
    ALLOWED_NAMESPACES: Comma-separated list of allowed namespaces
    VALIDATION_ENABLED: Enable/disable validation (default: true)

Usage:
    processor = create_manifest_processor_from_env()
    response = await processor.process_manifest_submission(request)
"""

from __future__ import annotations

import copy
import hashlib
import logging
import os
import re
from typing import Any, cast

import yaml
from kubernetes import client, config, dynamic
from kubernetes.client.models import V1Job
from kubernetes.client.rest import ApiException
from kubernetes.dynamic.exceptions import ResourceNotFoundError

from gco.models import (
    ManifestSubmissionRequest,
    ManifestSubmissionResponse,
    ResourceStatus,
)
from gco.services.structured_logging import configure_structured_logging, sanitize_log_value

# <pyflowchart-code-diagram> BEGIN - auto-inserted, do not edit
# Generated at (UTC): 2026-07-18T01:03:40Z
# Flowchart(s) generated from this file:
#   * ``ManifestProcessor.apply_queued_job`` -> ``diagrams/code_diagrams/gco/services/manifest_processor.ManifestProcessor_apply_queued_job.html``
#     (PNG: ``diagrams/code_diagrams/gco/services/manifest_processor.ManifestProcessor_apply_queued_job.png``)
# Regenerate with ``python diagrams/code_diagrams/generate.py``.
# <pyflowchart-code-diagram> END


# NOTE: No logging.basicConfig() here. This module is imported by the CLI
# (cli/jobs.py, cli/commands/*_cmd.py) as a library for YAML loading helpers.
# Calling basicConfig() at import time would configure the root logger with
# INFO-level output, causing noisy botocore/urllib3 INFO messages on every
# CLI command. Container entry points (manifest_api.py) do their own
# basicConfig() call.
logger = logging.getLogger(__name__)


# Accelerator resource keys and their corresponding node taint keys. GCO
# nodepools taint accelerator nodes with these keys (authoritative list:
# regional_stack._ADDON_NODE_TOLERATIONS), so a job requesting one of these
# resources must carry a matching toleration or it will never schedule.
# Taint key == resource key for all three. Kept in sync with the mirror in
# gco/services/queue_processor.py::ACCELERATOR_TAINTS.
ACCELERATOR_TAINTS = ("nvidia.com/gpu", "aws.amazon.com/neuron", "vpc.amazonaws.com/efa")

# Authoritative resource-kind policy shared by the REST and SQS submission
# paths. Keep the fallback here so both services fail closed to the same set
# when ALLOWED_KINDS is not explicitly configured.
DEFAULT_ALLOWED_KINDS = (
    "Job",
    "CronJob",
    "Deployment",
    "StatefulSet",
    "DaemonSet",
    "Service",
    "ConfigMap",
    "Pod",
)

# Authoritative image-source defaults shared by the REST and SQS submission
# paths. Keep these centralized so missing deployment wiring cannot make either
# path weaker or let their allowlists drift independently.
DEFAULT_TRUSTED_REGISTRIES = (
    "docker.io",
    "gcr.io",
    "quay.io",
    "registry.k8s.io",
    "k8s.gcr.io",
    "public.ecr.aws",
    "nvcr.io",
)
DEFAULT_TRUSTED_DOCKERHUB_ORGS = (
    "nvidia",
    "pytorch",
    "rayproject",
    "tensorflow",
    "huggingface",
    "amazon",
    "bitnami",
    "gco",
)

# CRUD endpoints accept only these exact built-in, namespaced GVKs. A kind-only
# allowlist is insufficient because a custom API group can define the same kind
# name, and cluster-scoped resources must never be reachable through a
# namespace-shaped user endpoint.
RESOURCE_API_VERSIONS: dict[str, frozenset[str]] = {
    "Job": frozenset({"batch/v1"}),
    "CronJob": frozenset({"batch/v1"}),
    "Deployment": frozenset({"apps/v1"}),
    "StatefulSet": frozenset({"apps/v1"}),
    "DaemonSet": frozenset({"apps/v1"}),
    "Service": frozenset({"v1"}),
    "ConfigMap": frozenset({"v1"}),
    "Pod": frozenset({"v1"}),
}


class RetryableQueuedJobApplyError(RuntimeError):
    """A deterministic queued Job apply can be retried or adopted safely."""


class QueuedJobNotCreatedError(ValueError):
    """A queued Job was rejected before any Kubernetes operation began."""


def _is_retryable_kubernetes_api_error(error: ApiException) -> bool:
    """Classify throttling, server, and transport-like Kubernetes API failures."""
    try:
        status = int(error.status or 0)
    except TypeError, ValueError:
        status = 0
    return status == 0 or status in {408, 429} or status >= 500


def validate_resource_kind(
    manifest: dict[str, Any], allowed_kinds: set[str] | tuple[str, ...] = DEFAULT_ALLOWED_KINDS
) -> tuple[bool, str | None]:
    """Validate a manifest kind against the shared submission allowlist."""
    kind = manifest.get("kind", "")
    allowed = set(allowed_kinds)
    if kind not in allowed:
        return (
            False,
            f"Resource kind '{kind}' is not allowed. Allowed kinds: {sorted(allowed)}",
        )
    return True, None


def _positive_quantity(value: Any) -> bool:
    """True if a K8s resource quantity is present and greater than zero."""
    if value is None:
        return False
    try:
        return float(value) > 0
    except TypeError, ValueError:
        # A non-numeric quantity is still an explicit request.
        return True


def _toleration_matches(tolerations: list[dict[str, Any]], taint_key: str) -> bool:
    """Return True if *tolerations* tolerates the ``<taint_key>=true:NoSchedule`` taint.

    A toleration matches when its ``key`` equals *taint_key*, its effect is
    empty (matches all effects) or ``NoSchedule``, and it either uses
    ``operator: Exists`` or ``operator: Equal`` with ``value: "true"``.
    Kept in sync with the mirror in queue_processor._toleration_matches.
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


# ---------------------------------------------------------------------------
# YAML Alias Rejection Loader
# ---------------------------------------------------------------------------


class NoAliasSafeLoader(yaml.SafeLoader):
    """A YAML SafeLoader that rejects anchors and aliases.

    YAML anchors (``&anchor``) and aliases (``*anchor``) can be used to
    construct exponentially large data structures (billion-laughs attack).
    This loader raises an error when any alias is encountered, preventing
    such attacks at the parsing stage.
    """

    def compose_node(self, parent: Any, index: Any) -> Any:
        if self.check_event(yaml.AliasEvent):  # type: ignore[no-untyped-call]
            event = self.get_event()  # type: ignore[no-untyped-call]
            raise yaml.composer.ComposerError(
                None,
                None,
                "YAML aliases are not allowed "
                "(security policy: yaml_allow_aliases=false), "
                f"found alias *{event.anchor}",
                event.start_mark,
            )
        return super().compose_node(parent, index)


def safe_load_yaml(stream: str | Any, *, allow_aliases: bool = False) -> Any:
    """Load a single YAML document with optional alias rejection.

    Args:
        stream: YAML string or file-like object.
        allow_aliases: If False (default), reject YAML anchors/aliases.

    Returns:
        Parsed YAML document.

    Raises:
        yaml.YAMLError: If the document is invalid or contains aliases
            when ``allow_aliases`` is False.
    """
    loader_cls = yaml.SafeLoader if allow_aliases else NoAliasSafeLoader
    # Loader is always a SafeLoader subclass (SafeLoader or NoAliasSafeLoader),
    # so this is equivalent to yaml.safe_load. Bandit's B506 check does not
    # recognize the custom loader as safe.
    return yaml.load(stream, Loader=loader_cls)  # nosec B506


def safe_load_all_yaml(stream: str | Any, *, allow_aliases: bool = False) -> list[Any]:
    """Load all YAML documents from a stream with optional alias rejection.

    Args:
        stream: YAML string or file-like object.
        allow_aliases: If False (default), reject YAML anchors/aliases.

    Returns:
        List of parsed YAML documents (``None`` documents are skipped).

    Raises:
        yaml.YAMLError: If any document is invalid or contains aliases
            when ``allow_aliases`` is False.
    """
    loader_cls = yaml.SafeLoader if allow_aliases else NoAliasSafeLoader
    # Loader is always a SafeLoader subclass, so this is equivalent to
    # yaml.safe_load_all. Bandit's B506 check does not recognize the custom
    # loader as safe.
    return [
        doc
        for doc in yaml.load_all(stream, Loader=loader_cls)
        if doc is not None  # nosec B506
    ]


class ManifestProcessor:
    """
    Processes Kubernetes manifest submissions and applies them to the cluster
    """

    def __init__(self, cluster_id: str, region: str, config_dict: dict[str, Any]):
        self.cluster_id = cluster_id
        self.region = region
        self.config = config_dict

        # Initialize Kubernetes clients
        try:
            # Try to load in-cluster config first (when running in pod)
            config.load_incluster_config()
            logger.info("Loaded in-cluster Kubernetes configuration")
        except config.ConfigException:
            try:
                # Fall back to local kubeconfig (for development)
                config.load_kube_config()
                logger.info("Loaded local Kubernetes configuration")
            except config.ConfigException as e:
                logger.error(f"Failed to load Kubernetes configuration: {e}")
                raise

        # Initialize API clients
        self.api_client = client.ApiClient()
        self.api_client.configuration.request_timeout = int(os.environ.get("K8S_API_TIMEOUT", "30"))
        self.core_v1 = client.CoreV1Api()
        self.apps_v1 = client.AppsV1Api()
        self.batch_v1 = client.BatchV1Api()
        self.networking_v1 = client.NetworkingV1Api()
        self.custom_objects = client.CustomObjectsApi()

        # Dynamic client for CRDs - lazy initialized to avoid cluster connection during init
        self._dynamic_client: dynamic.DynamicClient | None = None

        # Timeout for Kubernetes API calls (seconds)
        self._k8s_timeout = int(os.environ.get("K8S_API_TIMEOUT", "30"))

        # Resource quotas and limits
        self.max_cpu_per_manifest = self._parse_cpu_string(
            config_dict.get("max_cpu_per_manifest", "10")
        )
        self.max_memory_per_manifest = self._parse_memory_string(
            config_dict.get("max_memory_per_manifest", "32Gi")
        )
        self.max_gpu_per_manifest = int(config_dict.get("max_gpu_per_manifest", 4))
        # Hard-reject accelerator jobs that lack a matching node toleration.
        # Kept in sync with queue_processor.REQUIRE_ACCELERATOR_TOLERATION.
        self.require_accelerator_toleration = config_dict.get(
            "require_accelerator_toleration", True
        )
        self.allowed_namespaces = set(config_dict.get("allowed_namespaces", ["gco-jobs"]))
        self.validation_enabled = config_dict.get("validation_enabled", True)

        # Trusted registries for image validation (configurable via cdk.json)
        self.trusted_registries = config_dict.get(
            "trusted_registries", list(DEFAULT_TRUSTED_REGISTRIES)
        )
        self.trusted_dockerhub_orgs = config_dict.get(
            "trusted_dockerhub_orgs", list(DEFAULT_TRUSTED_DOCKERHUB_ORGS)
        )

        # Warn about trusted_registries entries that look like Docker Hub orgs (no dot or colon)
        for registry in self.trusted_registries:
            if not self._is_registry_domain(registry):
                logger.warning(
                    f"Trusted registry '{registry}' has no domain separator (dot or colon) — "
                    f"consider moving it to trusted_dockerhub_orgs instead"
                )

        # YAML parsing limits (configurable via cdk.json)
        self.yaml_max_depth = int(config_dict.get("yaml_max_depth", 50))

        # Allowed resource kinds (configurable via cdk.json)
        self.allowed_kinds = set(config_dict.get("allowed_kinds", DEFAULT_ALLOWED_KINDS))

        # Security policy — toggleable checks (configurable via cdk.json)
        security_policy = config_dict.get("manifest_security_policy", {})
        self.block_privileged = security_policy.get("block_privileged", True)
        self.block_privilege_escalation = security_policy.get("block_privilege_escalation", True)
        self.block_host_network = security_policy.get("block_host_network", True)
        self.block_host_pid = security_policy.get("block_host_pid", True)
        self.block_host_ipc = security_policy.get("block_host_ipc", True)
        self.block_host_path = security_policy.get("block_host_path", True)
        self.block_added_capabilities = security_policy.get("block_added_capabilities", True)
        self.block_run_as_root = security_policy.get("block_run_as_root", False)

    def _resource_access_error(self, api_version: str, kind: str, namespace: str) -> str | None:
        """Return an authorization error for a CRUD resource identifier."""
        if namespace not in self.allowed_namespaces:
            return f"Namespace '{namespace}' is not allowed"
        if kind not in self.allowed_kinds:
            return f"Resource kind '{kind}' is not allowed"
        allowed_versions = RESOURCE_API_VERSIONS.get(kind)
        if not allowed_versions or api_version not in allowed_versions:
            return (
                f"API version '{api_version}' is not allowed for kind '{kind}'. "
                f"Allowed versions: {sorted(allowed_versions or ())}"
            )
        return None

    # ------------------------------------------------------------------
    # Security defaults injection
    # ------------------------------------------------------------------

    @staticmethod
    def _extract_pod_spec(manifest: dict[str, Any]) -> dict[str, Any] | None:
        """Extract the pod spec from a manifest, handling all workload types.

        Supports:
        - Deployment / StatefulSet / DaemonSet / ReplicaSet → spec.template.spec
        - Job → spec.template.spec
        - CronJob → spec.jobTemplate.spec.template.spec
        - Bare Pod → spec (when ``containers`` key is present)

        Returns:
            The pod spec dict (mutable reference), or ``None`` if the manifest
            does not contain a recognisable pod spec.
        """
        spec = manifest.get("spec")
        if spec is None or not isinstance(spec, dict):
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

        # Deployment / StatefulSet / DaemonSet / ReplicaSet / Job:
        # spec.template.spec
        if "template" in spec:
            template = spec.get("template")
            if isinstance(template, dict):
                pod_spec = template.get("spec")
                if isinstance(pod_spec, dict):
                    return pod_spec
            return None

        # Bare Pod: spec contains "containers" directly
        if "containers" in spec:
            return cast(dict[str, Any], spec)

        return None

    def _inject_security_defaults(self, manifest: dict[str, Any]) -> dict[str, Any]:
        """Inject security defaults into user-submitted manifests.

        Currently injects:
        - ``automountServiceAccountToken: false`` in the pod spec (unless the
          user has explicitly set it).

        The method mutates *manifest* in-place and returns it for convenience.
        """
        pod_spec = self._extract_pod_spec(manifest)
        if pod_spec is not None:
            # Use setdefault so we don't override an explicit user choice
            pod_spec.setdefault("automountServiceAccountToken", False)
        return manifest

    @property
    def dynamic_client(self) -> dynamic.DynamicClient:
        """Lazy-initialized dynamic client for CRD support."""
        if self._dynamic_client is None:
            self._dynamic_client = dynamic.DynamicClient(self.api_client)
        return self._dynamic_client

    def _parse_cpu_string(self, cpu_str: str) -> int:
        """Parse CPU string to millicores"""
        if not cpu_str:
            return 0

        cpu_str = cpu_str.strip()
        if cpu_str.endswith("m"):
            return int(cpu_str[:-1])
        return int(cpu_str) * 1000

    def _parse_memory_string(self, memory_str: str) -> int:
        """Parse memory string to bytes"""
        if not memory_str:
            return 0

        memory_str = memory_str.strip()

        if memory_str.endswith("Ki"):
            return int(memory_str[:-2]) * 1024
        if memory_str.endswith("Mi"):
            return int(memory_str[:-2]) * 1024 * 1024
        if memory_str.endswith("Gi"):
            return int(memory_str[:-2]) * 1024 * 1024 * 1024
        if memory_str.endswith("Ti"):
            return int(memory_str[:-2]) * 1024 * 1024 * 1024 * 1024
        if memory_str.endswith("k"):
            return int(memory_str[:-1]) * 1000
        if memory_str.endswith("M"):
            return int(memory_str[:-1]) * 1000 * 1000
        if memory_str.endswith("G"):
            return int(memory_str[:-1]) * 1000 * 1000 * 1000
        return int(memory_str)

    def _check_yaml_depth(self, obj: Any, current_depth: int = 0) -> bool:
        """Check if a parsed YAML/JSON object exceeds max nesting depth.

        Recursively walks dicts and lists. Returns False if depth exceeds
        ``self.yaml_max_depth``.

        Args:
            obj: The parsed object to check (dict, list, or scalar).
            current_depth: Current recursion depth (callers should leave at 0).

        Returns:
            True if the object is within the depth limit, False otherwise.
        """
        if current_depth > self.yaml_max_depth:
            return False
        if isinstance(obj, dict):
            return all(self._check_yaml_depth(v, current_depth + 1) for v in obj.values())
        if isinstance(obj, list):
            return all(self._check_yaml_depth(item, current_depth + 1) for item in obj)
        return True

    def validate_manifest(
        self,
        manifest: dict[str, Any],
        default_namespace: str | None = None,
    ) -> tuple[bool, str | None]:
        """Validate a Kubernetes manifest for security and resource constraints.

        ``default_namespace`` is the request-level destination for manifests
        that omit ``metadata.namespace``. Validation and apply must resolve the
        same effective namespace or the request default could bypass the
        namespace allowlist.

        Returns: ``(is_valid, error_message)``.
        """
        if not self.validation_enabled:
            return True, None

        try:
            # YAML depth check — reject excessively nested documents
            if not self._check_yaml_depth(manifest):
                return (
                    False,
                    f"Manifest exceeds maximum nesting depth of {self.yaml_max_depth} levels",
                )

            # Basic structure validation
            required_fields = ["apiVersion", "kind", "metadata"]
            for field in required_fields:
                if field not in manifest:
                    return False, f"Missing required field: {field}"

            # Validate metadata
            metadata = manifest.get("metadata", {})
            if "name" not in metadata:
                return False, "Missing metadata.name field"

            # Validate namespace
            namespace = metadata.get("namespace", default_namespace or "gco-jobs")
            if namespace not in self.allowed_namespaces:
                return (
                    False,
                    f"Namespace '{namespace}' not allowed. Allowed namespaces: {list(self.allowed_namespaces)}",
                )

            # Validate resource kind using the policy shared with the SQS path.
            kind = manifest.get("kind", "")
            kind_valid, kind_error = validate_resource_kind(manifest, self.allowed_kinds)
            if not kind_valid:
                return False, kind_error

            # Validate resource limits for workload resources
            if kind in [
                "Deployment",
                "Job",
                "CronJob",
                "StatefulSet",
                "DaemonSet",
            ]:
                resource_valid, resource_error = self._validate_resource_limits(manifest)
                if not resource_valid:
                    return False, resource_error

            # Require accelerator jobs to carry a matching toleration.
            if self.require_accelerator_toleration:
                tol_valid, tol_error = self._validate_tolerations(manifest)
                if not tol_valid:
                    return False, tol_error

            # Security validations
            sec_valid, sec_error = self._validate_security_context(manifest)
            if not sec_valid:
                return False, f"Security context validation failed: {sec_error}"

            # Validate image sources (prevent pulling from untrusted registries)
            img_valid, img_error = self._validate_image_sources(manifest)
            if not img_valid:
                return False, img_error or "Untrusted image sources detected"

            return True, None

        except Exception as e:
            logger.error(f"Error validating manifest: {e}")
            return False, f"Validation error: {e!s}"

    def _validate_resource_limits(self, manifest: dict[str, Any]) -> tuple[bool, str]:
        """Validate resource limits in manifest.

        Returns:
            Tuple of (is_valid, error_message). error_message is empty if valid.
        """
        try:
            errors: list[str] = []
            spec = manifest.get("spec", {})

            # Get pod spec (handle different resource types)
            pod_spec = {}
            if "template" in spec:  # Deployment, StatefulSet, etc.
                pod_spec = spec.get("template", {}).get("spec", {})
            elif "jobTemplate" in spec:  # CronJob
                pod_spec = (
                    spec.get("jobTemplate", {}).get("spec", {}).get("template", {}).get("spec", {})
                )
            elif "containers" in spec:  # Pod
                pod_spec = spec

            total_cpu = 0
            total_memory = 0
            total_gpu = 0

            for _container_type, container in self._get_all_containers(pod_spec):
                resources = container.get("resources", {})
                requests = resources.get("requests", {})
                limits = resources.get("limits", {})

                # Check CPU (use limits if available, otherwise requests)
                cpu = limits.get("cpu") or requests.get(  # nosec B113 - dict.get(), not HTTP requests
                    "cpu", "0"
                )
                total_cpu += self._parse_cpu_string(cpu)

                # Check Memory
                memory = limits.get("memory") or requests.get(  # nosec B113 - dict.get(), not HTTP requests
                    "memory", "0"
                )
                total_memory += self._parse_memory_string(memory)

                # Check GPU
                gpu = limits.get("nvidia.com/gpu") or requests.get(  # nosec B113 - dict.get(), not HTTP requests
                    "nvidia.com/gpu", "0"
                )
                total_gpu += int(gpu)

            # Validate against limits
            if total_cpu > self.max_cpu_per_manifest:
                logger.warning(f"CPU limit exceeded: {total_cpu}m > {self.max_cpu_per_manifest}m")
                errors.append(f"CPU {total_cpu}m exceeds max {self.max_cpu_per_manifest}m")

            if total_memory > self.max_memory_per_manifest:
                logger.warning(
                    f"Memory limit exceeded: {total_memory} > {self.max_memory_per_manifest}"
                )
                mem_gb = self.max_memory_per_manifest / (1024**3)
                req_gb = total_memory / (1024**3)
                errors.append(f"Memory {req_gb:.0f}Gi exceeds max {mem_gb:.0f}Gi")

            if total_gpu > self.max_gpu_per_manifest:
                logger.warning(f"GPU limit exceeded: {total_gpu} > {self.max_gpu_per_manifest}")
                errors.append(f"GPU {total_gpu} exceeds max {self.max_gpu_per_manifest}")

            if errors:
                hint = (
                    "To raise limits, update resource_quotas in cdk.json "
                    "and redeploy (see examples/README.md#troubleshooting)"
                )
                return False, "; ".join(errors) + f". {hint}"

            return True, ""

        except Exception as e:
            logger.error(f"Error validating resource limits: {e}")
            return False, f"Resource limit validation error: {e}"

    def _validate_tolerations(self, manifest: dict[str, Any]) -> tuple[bool, str | None]:
        """Require accelerator jobs to carry a matching node toleration.

        GCO nodepools taint accelerator nodes with ``nvidia.com/gpu``,
        ``aws.amazon.com/neuron``, and ``vpc.amazonaws.com/efa`` (NoSchedule).
        A pod requesting one of these resources but lacking a matching
        toleration would stay Pending forever, so we reject it at admission
        with an actionable message instead.

        Returns:
            Tuple of (is_valid, error_message). error_message is None if valid.
        """
        pod_spec = self._extract_pod_spec(manifest)
        if not pod_spec:
            return True, None

        requested = self._requested_accelerators(pod_spec)
        if not requested:
            return True, None

        tolerations = pod_spec.get("tolerations", []) or []
        for taint in requested:
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
        return True, None

    def _requested_accelerators(self, pod_spec: dict[str, Any]) -> set[str]:
        """Return the set of accelerator taint keys any container requests
        a nonzero quantity of."""
        requested: set[str] = set()
        for _container_type, container in self._get_all_containers(pod_spec):
            resources = container.get("resources", {}) or {}
            for section in ("requests", "limits"):
                values = resources.get(section, {}) or {}
                for taint in ACCELERATOR_TAINTS:
                    if _positive_quantity(values.get(taint)):
                        requested.add(taint)
        return requested

    def _get_all_containers(self, pod_spec: dict[str, Any]) -> list[tuple[str, dict[str, Any]]]:
        """Get all containers from pod spec including init and ephemeral containers.

        Returns:
            List of (container_type, container_dict) tuples where container_type
            is one of 'container', 'initContainer', or 'ephemeralContainer'.
        """
        result = []
        for c in pod_spec.get("containers", []):
            result.append(("container", c))
        for c in pod_spec.get("initContainers", []):
            result.append(("initContainer", c))
        for c in pod_spec.get("ephemeralContainers", []):
            result.append(("ephemeralContainer", c))
        return result

    def _validate_security_context(self, manifest: dict[str, Any]) -> tuple[bool, str | None]:
        """Validate security context settings.

        Returns:
            Tuple of (is_valid, error_message). error_message is None if valid.
        """
        try:
            # Basic security checks - prevent privileged containers
            spec = manifest.get("spec", {})

            # Get pod spec (handle different resource types)
            pod_spec = None
            if "template" in spec:
                pod_spec = spec.get("template", {}).get("spec", {})
            elif "jobTemplate" in spec:
                pod_spec = (
                    spec.get("jobTemplate", {}).get("spec", {}).get("template", {}).get("spec", {})
                )
            elif "containers" in spec:
                pod_spec = spec

            if pod_spec:
                # --- Pod-level checks ---
                if self.block_host_network and pod_spec.get("hostNetwork", False):
                    return False, "hostNetwork is not permitted"

                if self.block_host_pid and pod_spec.get("hostPID", False):
                    return False, "hostPID is not permitted"

                if self.block_host_ipc and pod_spec.get("hostIPC", False):
                    return False, "hostIPC is not permitted"

                # Check volumes for hostPath
                if self.block_host_path:
                    for volume in pod_spec.get("volumes", []):
                        if volume.get("hostPath") is not None:
                            return False, "hostPath volumes are not permitted"

                # Check pod security context
                security_context = pod_spec.get("securityContext", {})
                if self.block_privileged and security_context.get("privileged", False):
                    return False, "privileged pod security context is not permitted"

                if self.block_run_as_root:
                    run_as_user = security_context.get("runAsUser")
                    if run_as_user is not None and run_as_user == 0:
                        return False, "running as root (runAsUser: 0) is not permitted"

                # --- Container-level checks ---
                for container_type, container in self._get_all_containers(pod_spec):
                    container_name = container.get("name", "unknown")
                    container_security = container.get("securityContext", {})
                    if self.block_privileged and container_security.get("privileged", False):
                        return (
                            False,
                            f"{container_type} '{container_name}': privileged containers are not permitted",
                        )
                    if self.block_privilege_escalation and container_security.get(
                        "allowPrivilegeEscalation", False
                    ):
                        return (
                            False,
                            f"{container_type} '{container_name}': allowPrivilegeEscalation is not permitted",
                        )

                    # Check for added capabilities
                    if self.block_added_capabilities:
                        added_caps = container_security.get("capabilities", {}).get("add", [])
                        if added_caps:
                            return (
                                False,
                                f"{container_type} '{container_name}': added capabilities are not permitted",
                            )

                    # Check for runAsUser: 0 (root) — off by default
                    if self.block_run_as_root:
                        run_as_user = container_security.get("runAsUser")
                        if run_as_user is not None and run_as_user == 0:
                            return (
                                False,
                                f"{container_type} '{container_name}': running as root (runAsUser: 0) is not permitted",
                            )

            return True, None

        except Exception as e:
            logger.error(f"Error validating security context: {e}")
            return False, f"Security context error: {e}"

    @staticmethod
    def _is_registry_domain(entry: str) -> bool:
        """Check if a registry entry is a proper domain (contains dot or colon).

        A proper registry domain contains either a dot (e.g., 'docker.io', 'gcr.io')
        or a colon (e.g., 'localhost:5000'). Entries without these are Docker Hub
        organization names (e.g., 'nvidia', 'gco').
        """
        return "." in entry or ":" in entry

    def _validate_image_sources(self, manifest: dict[str, Any]) -> tuple[bool, str | None]:
        """Validate container image sources.

        Uses proper domain matching instead of prefix matching to prevent
        dependency confusion attacks (e.g., 'gco-malicious/evil' should NOT
        match a trusted registry entry 'gco').

        Matching logic:
        1. If image has no '/' → official Docker Hub image (always allowed)
        2. If image has '/' and part before first '/' contains a dot or colon
           → it's a registry domain → match against trusted_registries
        3. If image has '/' but first segment has no dot/colon
           → it's a Docker Hub org → match against trusted_dockerhub_orgs
        4. Digest references (@sha256:) are accepted from any trusted source

        Returns:
            Tuple of (is_valid, error_message). error_message is None if valid.
        """
        try:
            trusted_registries = self.trusted_registries
            trusted_dockerhub_orgs = self.trusted_dockerhub_orgs

            spec = manifest.get("spec", {})

            # Get pod spec (handle different resource types)
            pod_spec = {}
            if "template" in spec:
                pod_spec = spec.get("template", {}).get("spec", {})
            elif "jobTemplate" in spec:
                pod_spec = (
                    spec.get("jobTemplate", {}).get("spec", {}).get("template", {}).get("spec", {})
                )
            elif "containers" in spec:
                pod_spec = spec

            for container_type, container in self._get_all_containers(pod_spec):
                image = container.get("image", "")
                if not image:
                    continue

                is_trusted = False

                # Case 1: Official Docker Hub image (no slash) — e.g., "python:3.14", "busybox"
                if "/" not in image:
                    is_trusted = True
                else:
                    # Image has a slash — determine if first segment is a domain or org
                    first_segment = image.split("/")[0]

                    if self._is_registry_domain(first_segment):
                        # Case 2: First segment looks like a domain (has dot or colon)
                        # Match against trusted_registries as exact domain match
                        for registry in trusted_registries:
                            if first_segment == registry:
                                is_trusted = True
                                break
                            # Also support multi-level registry paths like "public.ecr.aws"
                            # where the image might be "public.ecr.aws/lambda/python:3.14"
                            if image.startswith(registry + "/"):
                                is_trusted = True
                                break
                    else:
                        # Case 3: First segment has no dot/colon — it's a Docker Hub org
                        # Match against trusted_dockerhub_orgs
                        if first_segment in trusted_dockerhub_orgs:
                            is_trusted = True

                if not is_trusted:
                    container_name = container.get("name", "unknown")
                    # image comes from the user-submitted manifest; sanitize it
                    # before logging to prevent log injection / forging (CWE-117).
                    logger.warning("Untrusted image source: %s", sanitize_log_value(image))
                    return (
                        False,
                        f"{container_type} '{container_name}': Untrusted image source '{image}'",
                    )

            return True, None

        except Exception as e:
            logger.error(f"Error validating image sources: {e}")
            return False, f"Image source validation error: {e}"

    async def process_manifest_submission(
        self, request: ManifestSubmissionRequest
    ) -> ManifestSubmissionResponse:
        """
        Process a manifest submission request
        """
        logger.info(f"Processing manifest submission with {len(request.manifests)} manifests")

        resources = []
        errors = []
        overall_success = True

        try:
            # Process each manifest
            for i, manifest_data in enumerate(request.manifests):
                try:
                    # Validate manifest
                    is_valid, error_msg = self.validate_manifest(manifest_data, request.namespace)
                    if not is_valid:
                        error_msg = f"Manifest {i + 1} validation failed: {error_msg}"
                        errors.append(error_msg)
                        logger.error(error_msg)

                        # Create failed resource status
                        resource_status = ResourceStatus(
                            api_version=manifest_data.get("apiVersion", "unknown"),
                            kind=manifest_data.get("kind", "unknown"),
                            name=manifest_data.get("metadata", {}).get("name", f"manifest-{i + 1}"),
                            namespace=manifest_data.get("metadata", {}).get(
                                "namespace", request.namespace or "gco-jobs"
                            ),
                            status="failed",
                            message=error_msg,
                        )
                        resources.append(resource_status)
                        overall_success = False
                        continue

                    # Apply manifest if validation passed
                    if not request.dry_run:
                        resource_status = await self._apply_manifest(
                            manifest_data, request.namespace
                        )
                        resources.append(resource_status)

                        if not resource_status.is_successful():
                            overall_success = False
                    else:
                        # Dry run - just validate
                        resource_status = ResourceStatus(
                            api_version=manifest_data.get("apiVersion", "unknown"),
                            kind=manifest_data.get("kind", "unknown"),
                            name=manifest_data.get("metadata", {}).get("name", "unknown"),
                            namespace=manifest_data.get("metadata", {}).get(
                                "namespace", request.namespace or "gco-jobs"
                            ),
                            status="unchanged",
                            message="Dry run - validation passed",
                        )
                        resources.append(resource_status)

                except Exception as e:
                    error_msg = f"Error processing manifest {i + 1}: {e!s}"
                    errors.append(error_msg)
                    logger.error(error_msg)
                    overall_success = False

                    # Create failed resource status
                    resource_status = ResourceStatus(
                        api_version=manifest_data.get("apiVersion", "unknown"),
                        kind=manifest_data.get("kind", "unknown"),
                        name=manifest_data.get("metadata", {}).get("name", f"manifest-{i + 1}"),
                        namespace=manifest_data.get("metadata", {}).get(
                            "namespace", request.namespace or "gco-jobs"
                        ),
                        status="failed",
                        message=str(e),
                    )
                    resources.append(resource_status)

        except Exception as e:
            error_msg = f"Fatal error processing manifest submission: {e!s}"
            errors.append(error_msg)
            logger.error(error_msg)
            overall_success = False

        response = ManifestSubmissionResponse(
            success=overall_success,
            cluster_id=self.cluster_id,
            region=self.region,
            resources=resources,
            errors=errors if errors else None,
        )

        logger.info(
            f"Manifest submission completed - Success: {overall_success}, "
            f"Resources: {len(resources)}, Errors: {len(errors)}"
        )

        return response

    @staticmethod
    def queued_job_name(original_name: str, queue_job_id: str) -> str:
        """Return a DNS-label-safe Kubernetes name deterministically fenced by queue ID."""
        suffix = hashlib.sha256(queue_job_id.encode("utf-8")).hexdigest()[:16]
        prefix = re.sub(r"[^a-z0-9-]+", "-", original_name.lower()).strip("-")
        prefix = prefix[: 63 - len(suffix) - 1].rstrip("-") or "gco-job"
        return f"{prefix}-{suffix}"

    def apply_queued_job(
        self,
        manifest_data: dict[str, Any],
        namespace: str,
        queue_job_id: str,
    ) -> ResourceStatus:
        """Create or adopt exactly one deterministic ``batch/v1`` Job.

        This path deliberately bypasses generic manifest upsert semantics: it
        never deletes, renames, or replaces an existing Job. An ambiguous API
        result is safe to retry because the same queue ID always resolves to the
        same Kubernetes name and adoption requires the full queue ID annotation.
        """
        manifest = copy.deepcopy(manifest_data)
        if manifest.get("apiVersion") != "batch/v1" or manifest.get("kind") != "Job":
            raise QueuedJobNotCreatedError(
                "Central queue accepts only apiVersion 'batch/v1', kind 'Job'"
            )

        metadata = manifest.get("metadata")
        if not isinstance(metadata, dict):
            raise QueuedJobNotCreatedError("Queued Job metadata must be an object")
        declared_namespace = metadata.get("namespace")
        if declared_namespace is not None and declared_namespace != namespace:
            raise QueuedJobNotCreatedError("Queued Job namespace does not match the queue envelope")
        original_name = metadata.get("name")
        if not isinstance(original_name, str) or not original_name:
            raise QueuedJobNotCreatedError("Queued Job metadata.name is required")

        deterministic_name = self.queued_job_name(original_name, queue_job_id)
        metadata["name"] = deterministic_name
        metadata["namespace"] = namespace
        labels = metadata.setdefault("labels", {})
        annotations = metadata.setdefault("annotations", {})
        if not isinstance(labels, dict) or not isinstance(annotations, dict):
            raise QueuedJobNotCreatedError(
                "Queued Job metadata labels and annotations must be objects"
            )
        labels["gco.io/managed-by"] = "central-queue"
        labels["gco.io/queue-job-key"] = hashlib.sha256(queue_job_id.encode("utf-8")).hexdigest()[
            :32
        ]
        annotations["gco.io/queue-job-id"] = queue_job_id
        annotations["gco.io/original-job-name"] = original_name

        is_valid, validation_error = self.validate_manifest(manifest, namespace)
        if not is_valid:
            raise QueuedJobNotCreatedError(f"Queued Job validation failed: {validation_error}")
        self._inject_security_defaults(manifest)

        try:
            job = self.batch_v1.read_namespaced_job(
                name=deterministic_name,
                namespace=namespace,
                _request_timeout=self._k8s_timeout,
            )
            operation = "unchanged"
            message = "Existing deterministic Kubernetes Job adopted"
        except ApiException as error:
            if error.status != 404:
                if _is_retryable_kubernetes_api_error(error):
                    raise RetryableQueuedJobApplyError(
                        "Kubernetes Job lookup was inconclusive; retry deterministic adoption"
                    ) from error
                raise
            try:
                job = self.batch_v1.create_namespaced_job(
                    namespace=namespace,
                    body=manifest,
                    _request_timeout=self._k8s_timeout,
                )
                operation = "created"
                message = "Deterministic Kubernetes Job created"
            except ApiException as create_error:
                if create_error.status == 409:
                    try:
                        job = self.batch_v1.read_namespaced_job(
                            name=deterministic_name,
                            namespace=namespace,
                            _request_timeout=self._k8s_timeout,
                        )
                    except ApiException as adoption_error:
                        if adoption_error.status == 404 or _is_retryable_kubernetes_api_error(
                            adoption_error
                        ):
                            raise RetryableQueuedJobApplyError(
                                "Concurrent Kubernetes Job adoption was inconclusive"
                            ) from adoption_error
                        raise
                    except Exception as adoption_error:
                        raise RetryableQueuedJobApplyError(
                            "Concurrent Kubernetes Job adoption was inconclusive"
                        ) from adoption_error
                    operation = "unchanged"
                    message = "Concurrent deterministic Kubernetes Job adopted"
                elif _is_retryable_kubernetes_api_error(create_error):
                    raise RetryableQueuedJobApplyError(
                        "Kubernetes Job create result was inconclusive; retry deterministic adoption"
                    ) from create_error
                else:
                    raise
            except Exception as create_error:
                # A timeout or connection loss can occur after the API server
                # persisted the Job. Never mark that ambiguous result terminal.
                raise RetryableQueuedJobApplyError(
                    "Kubernetes Job create result was inconclusive; retry deterministic adoption"
                ) from create_error
        except Exception as read_error:
            raise RetryableQueuedJobApplyError(
                "Kubernetes Job lookup was inconclusive; retry deterministic adoption"
            ) from read_error

        actual_annotations = getattr(job.metadata, "annotations", None) or {}
        if actual_annotations.get("gco.io/queue-job-id") != queue_job_id:
            raise RuntimeError(
                f"Kubernetes Job name collision for {namespace}/{deterministic_name}"
            )
        uid = str(getattr(job.metadata, "uid", "") or "")
        actual_name = str(getattr(job.metadata, "name", "") or deterministic_name)
        actual_namespace = str(getattr(job.metadata, "namespace", "") or namespace)
        if not uid:
            raise RuntimeError("Kubernetes API returned a queued Job without a UID")
        return ResourceStatus(
            api_version="batch/v1",
            kind="Job",
            name=actual_name,
            namespace=actual_namespace,
            status=operation,
            message=message,
            uid=uid,
        )

    def read_queued_job(self, name: str, namespace: str) -> V1Job:
        """Read one reconciled Job through the processor's bounded client contract."""
        return self.batch_v1.read_namespaced_job(
            name=name,
            namespace=namespace,
            _request_timeout=self._k8s_timeout,
        )

    async def _apply_manifest(
        self, manifest_data: dict[str, Any], default_namespace: str | None = None
    ) -> ResourceStatus:
        """
        Apply a single manifest to the cluster.

        For Jobs and CronJobs, if the resource already exists and is completed/failed,
        it will be automatically deleted and recreated (since these resources are immutable).
        """
        try:
            api_version: str = manifest_data.get("apiVersion", "unknown")
            kind: str = manifest_data.get("kind", "unknown")
            metadata = manifest_data.get("metadata", {})
            name: str = metadata.get("name", "unknown")
            namespace: str = metadata.get("namespace", default_namespace or "gco-jobs")

            # Ensure namespace is set in manifest
            if "namespace" not in metadata and namespace:
                manifest_data["metadata"]["namespace"] = namespace

            # Inject security defaults (e.g., automountServiceAccountToken: false)
            self._inject_security_defaults(manifest_data)

            # Check if resource already exists
            existing_resource = await self._get_existing_resource(
                api_version, kind, name, namespace
            )

            if existing_resource:
                # Jobs are immutable — if one already exists and is finished,
                # delete it first so we can recreate cleanly.
                # If the job is still active, auto-rename to avoid collision.
                if kind == "Job":
                    if self._is_job_finished(existing_resource):
                        logger.info(
                            f"Job {name} already exists and is finished, deleting before recreating"
                        )
                        await self.delete_resource(api_version, kind, name, namespace)
                        import asyncio

                        await asyncio.sleep(1)
                        await self._create_resource(manifest_data)
                        status = "created"
                        message = "Previous completed job replaced with new submission"
                    else:
                        # Active job — rename to avoid destroying it
                        import uuid

                        suffix = uuid.uuid4().hex[:5]
                        new_name = f"{name}-{suffix}"
                        manifest_data["metadata"]["name"] = new_name
                        logger.warning(
                            f"Job {name} is still active, renamed new submission to {new_name}"
                        )
                        await self._create_resource(manifest_data)
                        status = "created"
                        message = (
                            f"Job '{name}' is still running. "
                            f"New submission renamed to '{new_name}'."
                        )
                        name = new_name
                else:
                    # Update existing resource (works for mutable resources)
                    updated_resource = await self._update_resource(manifest_data)
                    status = "updated" if updated_resource else "unchanged"
                    message = (
                        "Resource updated successfully" if updated_resource else "No changes needed"
                    )
            else:
                # Create new resource
                await self._create_resource(manifest_data)
                status = "created"
                message = "Resource created successfully"

            return ResourceStatus(
                api_version=api_version,
                kind=kind,
                name=name,
                namespace=namespace,
                status=status,
                message=message,
            )

        except ApiException as e:
            logger.error(f"Kubernetes API error applying manifest: {e}")
            return ResourceStatus(
                api_version=manifest_data.get("apiVersion", "unknown"),
                kind=manifest_data.get("kind", "unknown"),
                name=manifest_data.get("metadata", {}).get("name", "unknown"),
                namespace=manifest_data.get("metadata", {}).get("namespace", "gco-jobs"),
                status="failed",
                message=f"API error: {e.reason}",
            )
        except Exception as e:
            logger.error(f"Error applying manifest: {e}")
            return ResourceStatus(
                api_version=manifest_data.get("apiVersion", "unknown"),
                kind=manifest_data.get("kind", "unknown"),
                name=manifest_data.get("metadata", {}).get("name", "unknown"),
                namespace=manifest_data.get("metadata", {}).get("namespace", "gco-jobs"),
                status="failed",
                message=str(e),
            )

    def _is_job_finished(self, job_resource: dict[str, Any]) -> bool:
        """Check if a Kubernetes Job resource is in a terminal state (Complete or Failed)."""
        status = job_resource.get("status", {})
        conditions = status.get("conditions") or []
        for condition in conditions:
            cond_type = condition.get("type", "")
            cond_status = condition.get("status", "")
            if cond_type in ("Complete", "Failed") and cond_status == "True":
                return True
        return False

    async def _get_existing_resource(
        self,
        api_version: str,
        kind: str,
        name: str,
        namespace: str,
        *,
        api_resource: Any | None = None,
    ) -> dict[str, Any] | None:
        """Check if a resource already exists using dynamic client."""
        try:
            # Reuse an already-authorized discovery result when supplied so a
            # status request cannot observe a different resource definition
            # between the scope check and the actual read.
            if api_resource is None:
                api_resource = self._get_api_resource(api_version, kind)

            # Try to get the resource
            if namespace and api_resource.namespaced:
                resource = api_resource.get(name=name, namespace=namespace)
            else:
                resource = api_resource.get(name=name)

            if resource is not None:
                return dict(resource.to_dict())

        except ApiException as e:
            if e.status == 404:
                return None  # Resource doesn't exist
            raise
        except ValueError:
            # Unknown resource type
            return None

        return None

    def _get_api_resource(self, api_version: str, kind: str) -> Any:
        """Get the API resource for a given apiVersion and kind using dynamic client."""
        try:
            return self.dynamic_client.resources.get(api_version=api_version, kind=kind)
        except ResourceNotFoundError as e:
            logger.error(
                "Resource type not found: %s/%s",
                sanitize_log_value(api_version),
                sanitize_log_value(kind),
            )
            raise ValueError(f"Unknown resource type: {api_version}/{kind}") from e

    async def _create_resource(self, manifest_data: dict[str, Any]) -> Any:
        """Create a resource and return the API object, including server identity."""
        try:
            api_version = manifest_data.get("apiVersion", "")
            kind = manifest_data.get("kind", "")
            namespace = manifest_data.get("metadata", {}).get("namespace")

            api_resource = self._get_api_resource(api_version, kind)
            if namespace and api_resource.namespaced:
                return api_resource.create(body=manifest_data, namespace=namespace)
            return api_resource.create(body=manifest_data)
        except Exception as e:
            logger.error(f"Error creating resource: {e}")
            raise

    async def _update_resource(self, manifest_data: dict[str, Any]) -> bool:
        """Update an existing resource using dynamic client"""
        try:
            api_version = manifest_data.get("apiVersion", "")
            kind = manifest_data.get("kind", "")
            name = manifest_data.get("metadata", {}).get("name", "")
            namespace = manifest_data.get("metadata", {}).get("namespace")

            # Get the API resource
            api_resource = self._get_api_resource(api_version, kind)

            # Update the resource using patch (server-side apply)
            if namespace and api_resource.namespaced:
                api_resource.patch(
                    body=manifest_data,
                    name=name,
                    namespace=namespace,
                    content_type="application/merge-patch+json",
                )
            else:
                api_resource.patch(
                    body=manifest_data,
                    name=name,
                    content_type="application/merge-patch+json",
                )

            return True
        except Exception as e:
            logger.error(f"Error updating resource: {e}")
            raise

    async def delete_resource(
        self, api_version: str, kind: str, name: str, namespace: str
    ) -> ResourceStatus:
        """
        Delete a resource from the cluster using dynamic client
        """
        access_error = self._resource_access_error(api_version, kind, namespace)
        if access_error:
            return ResourceStatus(
                api_version=api_version,
                kind=kind,
                name=name,
                namespace=namespace,
                status="forbidden",
                message=access_error,
            )

        try:
            # Get the API resource
            api_resource = self._get_api_resource(api_version, kind)
            if not api_resource.namespaced:
                return ResourceStatus(
                    api_version=api_version,
                    kind=kind,
                    name=name,
                    namespace=namespace,
                    status="forbidden",
                    message="Cluster-scoped resource operations are not allowed",
                )

            # Every authorized GVK is namespaced; never drop the namespace.
            api_resource.delete(name=name, namespace=namespace)

            return ResourceStatus(
                api_version=api_version,
                kind=kind,
                name=name,
                namespace=namespace,
                status="deleted",
                message="Resource deleted successfully",
            )

        except ValueError as e:
            # Unknown resource type
            return ResourceStatus(
                api_version=api_version,
                kind=kind,
                name=name,
                namespace=namespace,
                status="failed",
                message=str(e),
            )
        except ApiException as e:
            if e.status == 404:
                return ResourceStatus(
                    api_version=api_version,
                    kind=kind,
                    name=name,
                    namespace=namespace,
                    status="unchanged",
                    message="Resource not found (already deleted)",
                )
            return ResourceStatus(
                api_version=api_version,
                kind=kind,
                name=name,
                namespace=namespace,
                status="failed",
                message=f"Delete failed: {e.reason}",
            )
        except Exception as e:
            return ResourceStatus(
                api_version=api_version,
                kind=kind,
                name=name,
                namespace=namespace,
                status="failed",
                message=str(e),
            )

    async def list_jobs(
        self, namespace: str | None = None, status_filter: str | None = None
    ) -> list[dict[str, Any]]:
        """
        List Kubernetes Jobs from allowed namespaces.

        Args:
            namespace: Filter by specific namespace (must be in allowed_namespaces)
            status_filter: Filter by status: "running", "completed", "failed"

        Returns:
            List of job dictionaries with metadata and status
        """
        jobs = []

        # Determine which namespaces to query
        if namespace:
            if namespace not in self.allowed_namespaces:
                raise ValueError(
                    f"Namespace '{namespace}' not allowed. "
                    f"Allowed namespaces: {list(self.allowed_namespaces)}"
                )
            namespaces_to_query = [namespace]
        else:
            namespaces_to_query = list(self.allowed_namespaces)

        for ns in namespaces_to_query:
            try:
                job_list = self.batch_v1.list_namespaced_job(
                    namespace=ns, _request_timeout=self._k8s_timeout
                )
                for job in job_list.items:
                    job_dict = self._job_to_dict(job)

                    # Apply status filter
                    if status_filter:
                        job_status = self._get_job_status(job)
                        if job_status != status_filter:
                            continue

                    jobs.append(job_dict)
            except ApiException as e:
                logger.warning(
                    "Failed to list jobs in namespace %s: %s", sanitize_log_value(ns), e.reason
                )
                continue

        return jobs

    def _job_to_dict(self, job: V1Job) -> dict[str, Any]:
        """Convert a Kubernetes Job object to a dictionary."""
        metadata = job.metadata
        status = job.status
        spec = job.spec

        return {
            "metadata": {
                "name": metadata.name,
                "namespace": metadata.namespace,
                "creationTimestamp": (
                    metadata.creation_timestamp.isoformat() if metadata.creation_timestamp else None
                ),
                "labels": metadata.labels or {},
                "uid": metadata.uid,
            },
            "spec": {
                "parallelism": spec.parallelism,
                "completions": spec.completions,
                "backoffLimit": spec.backoff_limit,
            },
            "status": {
                "active": status.active or 0,
                "succeeded": status.succeeded or 0,
                "failed": status.failed or 0,
                "startTime": status.start_time.isoformat() if status.start_time else None,
                "completionTime": (
                    status.completion_time.isoformat() if status.completion_time else None
                ),
                "conditions": [
                    {
                        "type": c.type,
                        "status": c.status,
                        "reason": c.reason,
                        "message": c.message,
                    }
                    for c in (status.conditions or [])
                ],
            },
        }

    def _get_job_status(self, job: V1Job) -> str:
        """Determine the status of a job: running, completed, or failed."""
        status = job.status
        conditions = status.conditions or []

        for condition in conditions:
            if condition.type == "Complete" and condition.status == "True":
                return "completed"
            if condition.type == "Failed" and condition.status == "True":
                return "failed"

        if (status.active or 0) > 0:
            return "running"

        return "pending"

    async def get_resource_status(
        self, api_version: str, kind: str, name: str, namespace: str
    ) -> dict[str, Any] | None:
        """
        Get the status of a specific resource
        """
        access_error = self._resource_access_error(api_version, kind, namespace)
        if access_error:
            return {
                "api_version": api_version,
                "kind": kind,
                "name": name,
                "namespace": namespace,
                "exists": False,
                "forbidden": True,
                "error": access_error,
            }

        try:
            api_resource = self._get_api_resource(api_version, kind)
            if not api_resource.namespaced:
                return {
                    "api_version": api_version,
                    "kind": kind,
                    "name": name,
                    "namespace": namespace,
                    "exists": False,
                    "forbidden": True,
                    "error": "Cluster-scoped resource operations are not allowed",
                }
            resource = await self._get_existing_resource(
                api_version,
                kind,
                name,
                namespace,
                api_resource=api_resource,
            )
            if resource:
                return {
                    "api_version": api_version,
                    "kind": kind,
                    "name": name,
                    "namespace": namespace,
                    "exists": True,
                    "status": resource.get("status", {}),
                    "metadata": resource.get("metadata", {}),
                    "spec": resource.get("spec", {}),
                }
            return {
                "api_version": api_version,
                "kind": kind,
                "name": name,
                "namespace": namespace,
                "exists": False,
            }
        except Exception as e:
            logger.error(f"Error getting resource status: {e}")
            return None


def _env_bool(name: str, default: bool) -> bool:
    """Parse a boolean environment variable using the SQS worker semantics."""
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    return raw.strip().lower() in {"true", "1", "yes", "on"}


def _manifest_security_policy_from_env() -> dict[str, bool]:
    return {
        "block_privileged": _env_bool("BLOCK_PRIVILEGED", True),
        "block_privilege_escalation": _env_bool("BLOCK_PRIVILEGE_ESCALATION", True),
        "block_host_network": _env_bool("BLOCK_HOST_NETWORK", True),
        "block_host_pid": _env_bool("BLOCK_HOST_PID", True),
        "block_host_ipc": _env_bool("BLOCK_HOST_IPC", True),
        "block_host_path": _env_bool("BLOCK_HOST_PATH", True),
        "block_added_capabilities": _env_bool("BLOCK_ADDED_CAPABILITIES", True),
        "block_run_as_root": _env_bool("BLOCK_RUN_AS_ROOT", False),
    }


def create_manifest_processor_from_env() -> ManifestProcessor:
    """
    Create ManifestProcessor instance from environment variables
    """
    cluster_id = os.getenv("CLUSTER_NAME", "unknown-cluster")
    region = os.getenv("REGION", "unknown-region")

    # Enable structured JSON logging for CloudWatch Insights
    configure_structured_logging(
        service_name="manifest-processor",
        cluster_id=cluster_id,
        region=region,
    )

    # Load configuration from environment
    config_dict = {
        "max_cpu_per_manifest": os.getenv("MAX_CPU_PER_MANIFEST", "10"),
        "max_memory_per_manifest": os.getenv("MAX_MEMORY_PER_MANIFEST", "32Gi"),
        "max_gpu_per_manifest": int(os.getenv("MAX_GPU_PER_MANIFEST", "4")),
        "require_accelerator_toleration": os.getenv(
            "REQUIRE_ACCELERATOR_TOLERATION", "true"
        ).lower()
        == "true",
        "allowed_namespaces": (
            ["gco-jobs"]
            if os.getenv("ALLOWED_NAMESPACES") is None
            else [
                namespace.strip()
                for namespace in os.environ["ALLOWED_NAMESPACES"].split(",")
                if namespace.strip()
            ]
        ),
        "validation_enabled": os.getenv("VALIDATION_ENABLED", "true").lower() == "true",
        "yaml_max_depth": int(os.getenv("YAML_MAX_DEPTH", "50")),
        "manifest_security_policy": _manifest_security_policy_from_env(),
    }

    allowed_kinds_env = os.getenv("ALLOWED_KINDS")
    if allowed_kinds_env is not None:
        # An absent variable uses the authoritative defaults; an explicitly
        # empty value is a deliberate deny-all policy and must stay empty.
        config_dict["allowed_kinds"] = [
            kind.strip() for kind in allowed_kinds_env.split(",") if kind.strip()
        ]

    # Image registry allowlist — sourced from the same CDK env vars the
    # queue_processor reads, so an attacker who holds sqs:SendMessage on
    # the regional queue can't reach an image source the REST path
    # rejects. When unset (or empty) the ManifestProcessor falls back
    # to its hardcoded default. Empty/missing values are dropped to
    # match the queue_processor's parsing rules.
    trusted_registries_env = os.getenv("TRUSTED_REGISTRIES", "")
    trusted_registries = [r.strip() for r in trusted_registries_env.split(",") if r.strip()]
    if trusted_registries:
        config_dict["trusted_registries"] = trusted_registries

    trusted_dockerhub_orgs_env = os.getenv("TRUSTED_DOCKERHUB_ORGS", "")
    trusted_dockerhub_orgs = [o.strip() for o in trusted_dockerhub_orgs_env.split(",") if o.strip()]
    if trusted_dockerhub_orgs:
        config_dict["trusted_dockerhub_orgs"] = trusted_dockerhub_orgs

    return ManifestProcessor(cluster_id, region, config_dict)
