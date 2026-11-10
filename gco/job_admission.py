"""Pure job-admission policy checks, free of Kubernetes and AWS clients.

Everything here is a function of a manifest and a policy, nothing else. That
constraint is the point: the same checks have to run in four places that cannot
share a client.

  * the manifest-processor REST service, at submission time
  * the SQS queue processor, when it drains a queued manifest
  * ``gco jobs check-policy``, against a policy read back over HTTP from a
    deployed region
  * the offline example validator, against caps read out of ``cdk.json``

Before this module existed the first two shared these helpers by importing them
from ``gco.services.manifest_processor``, which meant importing ``kubernetes``
to ask a question about a dict. The offline validator did exactly that. Worse,
every check that needed deployment state was a *method* reading ``self``, so a
caller holding a policy document rather than a live processor had no way in and
the only option was to reimplement the rule. A second implementation of an
admission rule is a rule that will drift, and it drifts silently -- the copy
keeps passing while the real gate changes underneath it.

So the deployment-dependent checks take a :class:`JobValidationPolicy` instead
of ``self``, and ``ManifestProcessor`` builds one from its own attributes and
delegates. There is one implementation of each rule, and the offline and
multi-region callers exercise the same code the cluster does.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Collection
from typing import Any, NamedTuple

from gco.hyperpod_config import (
    CAPACITY_PROVIDER_HYPERPOD,
    CAPACITY_PROVIDER_LABEL,
    HYPERPOD_LOCAL_QUEUE,
    HYPERPOD_TAINT_KEY,
)
from gco.services.structured_logging import sanitize_log_value

logger = logging.getLogger(__name__)


# Accelerator resource keys and their corresponding node taint keys. GCO
# nodepools taint accelerator nodes with these keys (authoritative list:
# regional_stack._ADDON_NODE_TOLERATIONS), so a job requesting one of these
# resources must carry a matching toleration or it will never schedule.
# Taint key == resource key for all three. Kept in sync with the mirror in
# gco/services/queue_processor.py::ACCELERATOR_TAINTS.
ACCELERATOR_TAINTS = ("nvidia.com/gpu", "aws.amazon.com/neuron", "vpc.amazonaws.com/efa")
# The Kueue label that routes a workload to a LocalQueue. A Job labelled for
# the HyperPod queue (gco.hyperpod_config.HYPERPOD_LOCAL_QUEUE) is admitted onto
# the HyperPod ResourceFlavor, which injects the HyperPod node selector and
# toleration itself.
KUEUE_QUEUE_NAME_LABEL = "kueue.x-k8s.io/queue-name"
# Exact pinned group/version for the Kubeflow Trainer v2 TrainJob kind.
# Kept in lockstep with the kubeflow-trainer chart in
# lambda/helm-installer/charts.yaml and the extracted runtime manifest in
# lambda/kubectl-applier-simple/manifests/.
TRAINJOB_API_VERSION = "trainer.kubeflow.org/v1alpha1"
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
    "TrainJob",
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
    # Org-scoped prefixes such as ``ghcr.io/<org>`` are honoured through the
    # startswith branch of the matcher when an operator adds them to
    # cdk.json job_validation_policy.trusted_registries; none ships by
    # default since no example needs one.
)
DEFAULT_TRUSTED_DOCKERHUB_ORGS = (
    "nvidia",
    "pytorch",
    "rayproject",
    "tensorflow",
    "huggingface",
    "amazon",
    "bitnami",
    # Official orgs of the vLLM and SGLang projects — the images shipped in
    # examples/inference-vllm.yaml and examples/inference-sglang.yaml. Kept in
    # lockstep with cdk.json job_validation_policy.trusted_dockerhub_orgs (see
    # tests/test_manifest_processor_extended.py).
    "vllm",
    "lmsysorg",
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
    "TrainJob": frozenset({TRAINJOB_API_VERSION}),
}
# Actionable guidance when an allowed kind's CRD/controller is absent from
# the cluster. Without this, a policy-allowed manifest whose addon is
# disabled fails with an unactionable "Unknown resource type" — or worse,
# is accepted and never reconciles.
ADDON_KIND_HINTS: dict[str, str] = {
    "TrainJob": (
        "TrainJob requires the kubeflow-trainer addon; enable "
        'helm.kubeflow_trainer ("enabled": true) in cdk.json and redeploy '
        "the regional stack"
    ),
}


def _extract_validation_pod_spec(manifest: dict[str, Any]) -> dict[str, Any]:
    """Locate the pod spec for image-source validation across resource shapes."""
    spec = manifest.get("spec", {})
    pod_spec: Any = {}
    if "template" in spec:
        pod_spec = spec.get("template", {}).get("spec", {})
    elif "jobTemplate" in spec:
        pod_spec = spec.get("jobTemplate", {}).get("spec", {}).get("template", {}).get("spec", {})
    elif "containers" in spec:
        pod_spec = spec
    return pod_spec if isinstance(pod_spec, dict) else {}


class TrainJobPodSpecs(NamedTuple):
    """Every pod-spec-shaped view a TrainJob manifest can cause to run.

    A TrainJob has no ``spec.template``: its pods come from the referenced
    ClusterTrainingRuntime, customized by first-class fields
    (``spec.trainer``) and by arbitrary runtime patches
    (``spec.runtimePatches[].trainingRuntimeSpec`` — which can nest complete
    pod specs, including containers, volumes, and security contexts).
    Validating only the classic single-pod-spec shapes would let every
    image-trust, security-context, and resource-cap check pass vacuously,
    so TrainJob validation runs over this decomposition instead.

    Attributes:
        trainer: A synthetic pod spec carrying ``spec.trainer``'s image and
            per-node resources, or ``None`` when neither is set (the runtime
            defaults then apply — our shipped runtimes pin trusted images).
        embedded: Every dict carrying a container list found anywhere under
            ``spec`` (today that means inside ``runtimePatches``); these are
            live references into the manifest, so security-default injection
            through them mutates the manifest.
        num_nodes: ``spec.trainer.numNodes`` (minimum 1) — the replica
            multiplier for the trainer spec's resource totals.
    """

    trainer: dict[str, Any] | None
    embedded: list[dict[str, Any]]
    num_nodes: int


def _collect_embedded_pod_specs(node: Any, found: list[dict[str, Any]]) -> None:
    """Recursively collect every dict that carries a container list.

    Walking the whole structure — rather than enumerating known patch paths —
    means a future TrainJob field that can smuggle a container is validated
    by default instead of silently skipped.
    """
    if isinstance(node, dict):
        if any(
            isinstance(node.get(key), list)
            for key in ("containers", "initContainers", "ephemeralContainers")
        ):
            found.append(node)
        for value in node.values():
            _collect_embedded_pod_specs(value, found)
    elif isinstance(node, list):
        for item in node:
            _collect_embedded_pod_specs(item, found)


def extract_trainjob_pod_specs(manifest: dict[str, Any]) -> TrainJobPodSpecs:
    """Decompose a TrainJob manifest into validatable pod-spec views.

    Shared by the REST and SQS submission paths (the queue processor imports
    this) so TrainJob validation semantics cannot drift between them.
    """
    spec = manifest.get("spec")
    spec = spec if isinstance(spec, dict) else {}
    trainer = spec.get("trainer")
    trainer = trainer if isinstance(trainer, dict) else {}

    pseudo: dict[str, Any] | None = None
    if trainer.get("image") or isinstance(trainer.get("resourcesPerNode"), dict):
        container: dict[str, Any] = {"name": "trainer"}
        if trainer.get("image"):
            container["image"] = trainer["image"]
        if isinstance(trainer.get("resourcesPerNode"), dict):
            container["resources"] = {
                "requests": trainer["resourcesPerNode"].get("requests", {}) or {},
                "limits": trainer["resourcesPerNode"].get("limits", {}) or {},
            }
        pseudo = {"containers": [container]}

    embedded: list[dict[str, Any]] = []
    _collect_embedded_pod_specs(spec, embedded)

    raw_nodes = trainer.get("numNodes")
    try:
        num_nodes = max(1, int(raw_nodes)) if raw_nodes is not None else 1
    except TypeError, ValueError:
        num_nodes = 1
    return TrainJobPodSpecs(trainer=pseudo, embedded=embedded, num_nodes=num_nodes)


def trainjob_validation_pod_specs(manifest: dict[str, Any]) -> list[dict[str, Any]]:
    """All pod-spec views of a TrainJob, synthetic trainer spec first."""
    specs = extract_trainjob_pod_specs(manifest)
    views = [specs.trainer] if specs.trainer is not None else []
    views.extend(specs.embedded)
    return views


def _iter_all_containers(pod_spec: dict[str, Any]) -> list[tuple[str, dict[str, Any]]]:
    """All (container_type, container) pairs incl. init and ephemeral containers."""
    result: list[tuple[str, dict[str, Any]]] = []
    for container in pod_spec.get("containers", []):
        result.append(("container", container))
    for container in pod_spec.get("initContainers", []):
        result.append(("initContainer", container))
    for container in pod_spec.get("ephemeralContainers", []):
        result.append(("ephemeralContainer", container))
    return result


def _is_trusted_registry_domain(entry: str) -> bool:
    """True when a registry entry is a domain (dot/colon) rather than a Hub org."""
    return "." in entry or ":" in entry


def validate_image_sources(
    manifest: dict[str, Any],
    trusted_registries: list[str] | tuple[str, ...] = DEFAULT_TRUSTED_REGISTRIES,
    trusted_dockerhub_orgs: list[str] | tuple[str, ...] = DEFAULT_TRUSTED_DOCKERHUB_ORGS,
) -> tuple[bool, str | None]:
    """Validate container image sources against the trust allowlists.

    Pure function (no Kubernetes client, no configuration loading) so offline
    validators — the example-manifest static checks, tests — apply the exact
    logic the deployed services enforce. ``ManifestProcessor`` delegates here.

    Matching logic:
    1. No ``/`` in the image → official Docker Hub image (always allowed)
    2. First segment contains a dot/colon → registry domain → exact match or
       org-scoped prefix match against ``trusted_registries``
    3. Otherwise → Docker Hub org → match against ``trusted_dockerhub_orgs``

    TrainJob manifests carry no single pod spec; every image they can run —
    ``spec.trainer.image`` plus any container smuggled in through
    ``runtimePatches`` — is validated through the TrainJob decomposition.
    """
    try:
        if manifest.get("kind") == "TrainJob":
            pod_specs = trainjob_validation_pod_specs(manifest)
        else:
            pod_specs = [_extract_validation_pod_spec(manifest)]
        for pod_spec in pod_specs:
            failure = _untrusted_container_in_pod_spec(
                pod_spec, trusted_registries, trusted_dockerhub_orgs
            )
            if failure is not None:
                return False, failure
        return True, None
    except Exception as e:
        logger.error(f"Error validating image sources: {e}")
        return False, f"Image source validation error: {e}"


def _untrusted_container_in_pod_spec(
    pod_spec: dict[str, Any],
    trusted_registries: list[str] | tuple[str, ...],
    trusted_dockerhub_orgs: list[str] | tuple[str, ...],
) -> str | None:
    """Return the failure message for the first untrusted image, or None."""
    for container_type, container in _iter_all_containers(pod_spec):
        image = container.get("image", "")
        if not image:
            continue
        is_trusted = False
        if "/" not in image:
            is_trusted = True
        else:
            first_segment = image.split("/")[0]
            if _is_trusted_registry_domain(first_segment):
                for registry in trusted_registries:
                    if first_segment == registry or image.startswith(registry + "/"):
                        is_trusted = True
                        break
            elif first_segment in trusted_dockerhub_orgs:
                is_trusted = True
        if not is_trusted:
            container_name = container.get("name", "unknown")
            # image comes from the user-submitted manifest; sanitize it
            # before logging to prevent log injection / forging (CWE-117).
            logger.warning("Untrusted image source: %s", sanitize_log_value(image))
            return f"{container_type} '{container_name}': Untrusted image source '{image}'"
    return None


def validate_resource_kind(
    manifest: dict[str, Any],
    allowed_kinds: Collection[str] = DEFAULT_ALLOWED_KINDS,
) -> tuple[bool, str | None]:
    """Validate a manifest kind against the shared submission allowlist.

    ``allowed_kinds`` is any collection because the allowlist arrives as a tuple
    of defaults, a set off a live processor, and a frozenset off a
    :class:`JobValidationPolicy`; only membership and ordering-for-display are
    used, so narrowing the type would force callers into pointless conversions.
    """
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
# Quantity parsing
# ---------------------------------------------------------------------------
# Deliberately the same narrow parsers the admission path has always used,
# rather than gco.resource_governance.parse_k8s_quantity. They round and reject
# differently at the edges (a bare float CPU, an exponent suffix), and a
# pre-submit check that parses more permissively than the gate would call a job
# admissible that the gate then rejects. Matching the gate matters more here
# than being the better parser.


def parse_cpu_millicores(cpu_str: str) -> int:
    """Parse a Kubernetes CPU quantity to millicores."""
    if not cpu_str:
        return 0
    cpu_str = cpu_str.strip()
    if cpu_str.endswith("m"):
        return int(cpu_str[:-1])
    return int(cpu_str) * 1000


def parse_memory_bytes(memory_str: str) -> int:
    """Parse a Kubernetes memory quantity to bytes."""
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


def extract_pod_spec(manifest: dict[str, Any]) -> dict[str, Any] | None:
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
        return spec
    return None


def requested_accelerators(pod_spec: dict[str, Any]) -> set[str]:
    """Return the accelerator taint keys any container requests a nonzero
    quantity of."""
    requested: set[str] = set()
    for _container_type, container in _iter_all_containers(pod_spec):
        resources = container.get("resources", {}) or {}
        for section in ("requests", "limits"):
            values = resources.get(section, {}) or {}
            for taint in ACCELERATOR_TAINTS:
                if _positive_quantity(values.get(taint)):
                    requested.add(taint)
    return requested


def weighted_pod_specs(manifest: dict[str, Any]) -> list[tuple[dict[str, Any], int]]:
    """Return every pod spec in *manifest* paired with its replica multiplier.

    A TrainJob runs its trainer spec once per node, so the manifest's total is
    ``numNodes`` x the per-node request. Counting a 16-node GPU job as one node
    would make the per-manifest cap meaningless.
    """
    specs: list[tuple[dict[str, Any], int]] = []
    if manifest.get("kind") == "TrainJob":
        trainjob_specs = extract_trainjob_pod_specs(manifest)
        if trainjob_specs.trainer is not None:
            specs.append((trainjob_specs.trainer, trainjob_specs.num_nodes))
        specs.extend((item, 1) for item in trainjob_specs.embedded)
        return specs

    pod_spec = extract_pod_spec(manifest)
    specs.append((pod_spec if pod_spec is not None else {}, 1))
    return specs


# ---------------------------------------------------------------------------
# The policy a manifest is judged against
# ---------------------------------------------------------------------------


class JobValidationPolicy(NamedTuple):
    """Everything the admission checks need to know about a deployment.

    Immutable and client-free, so it can come from any of three sources that
    know progressively less:

    ``from_processor_attributes``
        what a live ManifestProcessor enforces right now. Authoritative.

    ``from_policy_document``
        the body of ``GET /api/v1/policy`` from a deployed region. Also
        authoritative -- that endpoint reads the same attributes -- but it
        arrives over the network, so it may be stale by the age of the response.

    ``from_cdk_context``
        ``cdk.json``'s ``job_validation_policy``. **Not** authoritative, and the
        gap is not hypothetical: CDK appends the project's own ECR registry
        hostnames to ``trusted_registries`` at synth time, so a deployed region
        trusts registries that appear nowhere in the file. A live run on
        2026-08-26 showed two such hostnames in the effective allowlist. The
        file also says nothing about which commit a region was deployed from.
        Callers using this source must say so, and must not turn a rejection
        into a hard failure.
    """

    max_cpu_millicores: int
    max_memory_bytes: int
    max_gpu_count: int
    allowed_namespaces: frozenset[str]
    allowed_kinds: frozenset[str]
    trusted_registries: tuple[str, ...]
    trusted_dockerhub_orgs: tuple[str, ...]
    require_accelerator_toleration: bool
    security: dict[str, bool]
    validation_enabled: bool = True
    yaml_max_depth: int = 50

    # -- constructors -----------------------------------------------------
    @classmethod
    def from_policy_document(cls, document: dict[str, Any]) -> JobValidationPolicy:
        """Build from a ``GET /api/v1/policy`` response body."""
        caps = document.get("manifest_caps", {}) or {}
        defaults = _default_security_flags()
        security = dict(defaults)
        security.update(
            {
                key: bool(value)
                for key, value in (document.get("manifest_security_policy", {}) or {}).items()
                if key in defaults
            }
        )
        return cls(
            max_cpu_millicores=int(caps.get("max_cpu_millicores", 0)),
            max_memory_bytes=int(caps.get("max_memory_bytes", 0)),
            max_gpu_count=int(caps.get("max_gpu_count", 0)),
            allowed_namespaces=frozenset(document.get("allowed_namespaces", ()) or ()),
            allowed_kinds=frozenset(document.get("allowed_kinds", ()) or ()),
            trusted_registries=tuple(sorted(document.get("trusted_registries", ()) or ())),
            trusted_dockerhub_orgs=tuple(sorted(document.get("trusted_dockerhub_orgs", ()) or ())),
            require_accelerator_toleration=bool(
                document.get("require_accelerator_toleration", True)
            ),
            security=security,
            validation_enabled=bool(document.get("validation_enabled", True)),
            yaml_max_depth=int(document.get("yaml_max_depth", 50)),
        )

    @classmethod
    def from_cdk_context(cls, job_validation_policy: dict[str, Any]) -> JobValidationPolicy:
        """Build from ``cdk.json``'s ``context.job_validation_policy``.

        Applies the same fallbacks the service applies when a key is absent, so
        an unset key reads as the shipped default rather than as zero.
        """
        from gco.resource_governance import DEFAULT_MANIFEST_RESOURCE_CAPS

        cfg = job_validation_policy or {}
        defaults = _default_security_flags()
        security = dict(defaults)
        security.update(
            {
                key: bool(value)
                for key, value in (cfg.get("manifest_security_policy", {}) or {}).items()
                if key in defaults
            }
        )
        return cls(
            max_cpu_millicores=parse_cpu_millicores(
                str(
                    cfg.get(
                        "max_cpu_per_manifest",
                        DEFAULT_MANIFEST_RESOURCE_CAPS["max_cpu_per_manifest"],
                    )
                )
            ),
            max_memory_bytes=parse_memory_bytes(
                str(
                    cfg.get(
                        "max_memory_per_manifest",
                        DEFAULT_MANIFEST_RESOURCE_CAPS["max_memory_per_manifest"],
                    )
                )
            ),
            max_gpu_count=int(
                cfg.get(
                    "max_gpu_per_manifest",
                    DEFAULT_MANIFEST_RESOURCE_CAPS["max_gpu_per_manifest"],
                )
            ),
            allowed_namespaces=frozenset(cfg.get("allowed_namespaces", ("gco-jobs",))),
            allowed_kinds=frozenset(cfg.get("allowed_kinds", DEFAULT_ALLOWED_KINDS)),
            trusted_registries=tuple(
                sorted(cfg.get("trusted_registries", DEFAULT_TRUSTED_REGISTRIES))
            ),
            trusted_dockerhub_orgs=tuple(
                sorted(cfg.get("trusted_dockerhub_orgs", DEFAULT_TRUSTED_DOCKERHUB_ORGS))
            ),
            require_accelerator_toleration=bool(cfg.get("require_accelerator_toleration", True)),
            security=security,
            validation_enabled=bool(cfg.get("validation_enabled", True)),
            yaml_max_depth=int(cfg.get("yaml_max_depth", 50)),
        )


def _default_security_flags() -> dict[str, bool]:
    """The shipped security-policy defaults, as a plain mutable dict."""
    from gco.manifest_security_policy import MANIFEST_SECURITY_POLICY_DEFAULTS

    return dict(MANIFEST_SECURITY_POLICY_DEFAULTS)


# ---------------------------------------------------------------------------
# The checks
# ---------------------------------------------------------------------------


def check_resource_caps(manifest: dict[str, Any], policy: JobValidationPolicy) -> tuple[bool, str]:
    """Check a manifest's aggregate CPU / memory / GPU against the caps.

    Returns ``(is_valid, error_message)``; the message is empty when valid.
    """
    try:
        errors: list[str] = []
        total_cpu = 0
        total_memory = 0
        total_gpu = 0

        for pod_spec, multiplier in weighted_pod_specs(manifest):
            for _container_type, container in _iter_all_containers(pod_spec):
                resources = container.get("resources", {})
                requests = resources.get("requests", {})
                limits = resources.get("limits", {})

                # Use limits if available, otherwise requests.
                cpu = limits.get("cpu") or requests.get("cpu", "0")  # nosec B113  # dict.get()
                total_cpu += multiplier * parse_cpu_millicores(cpu)

                memory = limits.get("memory") or requests.get("memory", "0")  # nosec B113
                total_memory += multiplier * parse_memory_bytes(memory)

                gpu = limits.get("nvidia.com/gpu") or requests.get("nvidia.com/gpu", "0")  # nosec B113
                total_gpu += multiplier * int(gpu)

        if total_cpu > policy.max_cpu_millicores:
            logger.warning(f"CPU limit exceeded: {total_cpu}m > {policy.max_cpu_millicores}m")
            errors.append(f"CPU {total_cpu}m exceeds max {policy.max_cpu_millicores}m")

        if total_memory > policy.max_memory_bytes:
            logger.warning(f"Memory limit exceeded: {total_memory} > {policy.max_memory_bytes}")
            mem_gb = policy.max_memory_bytes / (1024**3)
            req_gb = total_memory / (1024**3)
            errors.append(f"Memory {req_gb:.0f}Gi exceeds max {mem_gb:.0f}Gi")

        if total_gpu > policy.max_gpu_count:
            logger.warning(f"GPU limit exceeded: {total_gpu} > {policy.max_gpu_count}")
            errors.append(f"GPU {total_gpu} exceeds max {policy.max_gpu_count}")

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


def check_security_context(
    manifest: dict[str, Any], policy: JobValidationPolicy
) -> tuple[bool, str | None]:
    """Check pod- and container-level security settings against the policy.

    Returns ``(is_valid, error_message)``; the message is ``None`` when valid.
    """
    try:
        flags = policy.security
        spec = manifest.get("spec", {})

        # A TrainJob can nest complete pod specs inside runtimePatches, so every
        # one of them gets the full check — otherwise privileged containers or
        # hostPath volumes could ride in through a patch.
        pod_specs: list[dict[str, Any]]
        if manifest.get("kind") == "TrainJob":
            pod_specs = trainjob_validation_pod_specs(manifest)
        else:
            pod_spec = None
            if "template" in spec:
                pod_spec = spec.get("template", {}).get("spec", {})
            elif "jobTemplate" in spec:
                pod_spec = (
                    spec.get("jobTemplate", {}).get("spec", {}).get("template", {}).get("spec", {})
                )
            elif "containers" in spec:
                pod_spec = spec
            pod_specs = [pod_spec] if pod_spec else []

        for pod_spec in pod_specs:
            # --- Pod-level checks ---
            if flags.get("block_host_network") and pod_spec.get("hostNetwork", False):
                return False, "hostNetwork is not permitted"

            if flags.get("block_host_pid") and pod_spec.get("hostPID", False):
                return False, "hostPID is not permitted"

            if flags.get("block_host_ipc") and pod_spec.get("hostIPC", False):
                return False, "hostIPC is not permitted"

            if flags.get("block_host_path"):
                for volume in pod_spec.get("volumes", []):
                    if volume.get("hostPath") is not None:
                        return False, "hostPath volumes are not permitted"

            security_context = pod_spec.get("securityContext", {})
            if flags.get("block_privileged") and security_context.get("privileged", False):
                return False, "privileged pod security context is not permitted"

            if flags.get("block_run_as_root"):
                run_as_user = security_context.get("runAsUser")
                if run_as_user is not None and run_as_user == 0:
                    return False, "running as root (runAsUser: 0) is not permitted"

            # --- Container-level checks ---
            for container_type, container in _iter_all_containers(pod_spec):
                container_name = container.get("name", "unknown")
                container_security = container.get("securityContext", {})
                if flags.get("block_privileged") and container_security.get("privileged", False):
                    return (
                        False,
                        f"{container_type} '{container_name}': privileged containers are not permitted",
                    )
                if flags.get("block_privilege_escalation") and container_security.get(
                    "allowPrivilegeEscalation", False
                ):
                    return (
                        False,
                        f"{container_type} '{container_name}': allowPrivilegeEscalation is not permitted",
                    )

                if flags.get("block_added_capabilities"):
                    added_caps = container_security.get("capabilities", {}).get("add", [])
                    if added_caps:
                        return (
                            False,
                            f"{container_type} '{container_name}': added capabilities are not permitted",
                        )

                if flags.get("block_run_as_root"):
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


def hyperpod_routing(manifest: dict[str, Any], pod_specs: list[dict[str, Any]]) -> str | None:
    """How *manifest* reaches SageMaker HyperPod nodes, or ``None`` if it does not.

    ``"queue"``: the workload is labelled for the HyperPod Kueue LocalQueue
    (``kueue.x-k8s.io/queue-name: gco-hyperpod``); Kueue admits it onto the
    HyperPod ResourceFlavor, which injects the node selector and the
    toleration. ``"selector"``: every pod spec selects HyperPod nodes itself
    (``nodeSelector`` ``gco.aws/capacity-provider: hyperpod``), the "any other
    scheduler" path of docs/HYPERPOD.md, where the toleration is the author's
    job. Anything else, including a manifest only some of whose pod specs
    select HyperPod, is ``None``.
    """
    metadata = manifest.get("metadata")
    labels = (metadata.get("labels") if isinstance(metadata, dict) else None) or {}
    if isinstance(labels, dict) and labels.get(KUEUE_QUEUE_NAME_LABEL) == HYPERPOD_LOCAL_QUEUE:
        return "queue"
    if pod_specs and all(
        isinstance(spec.get("nodeSelector"), dict)
        and spec["nodeSelector"].get(CAPACITY_PROVIDER_LABEL) == CAPACITY_PROVIDER_HYPERPOD
        for spec in pod_specs
    ):
        return "selector"
    return None


def check_tolerations(
    manifest: dict[str, Any], policy: JobValidationPolicy | None = None
) -> tuple[bool, str | None]:
    """Require accelerator jobs to carry a matching node toleration.

    GCO nodepools taint accelerator nodes with ``nvidia.com/gpu``,
    ``aws.amazon.com/neuron``, and ``vpc.amazonaws.com/efa`` (NoSchedule). A pod
    requesting one of these resources but lacking a matching toleration would
    stay Pending forever, so we reject it at admission with an actionable
    message instead.

    SageMaker HyperPod nodes carry none of those taints; the one taint they
    carry is ``gco.aws/hyperpod=true:NoSchedule``. A workload routed to them
    (:func:`hyperpod_routing`) is therefore held to that taint instead: a Job
    labelled for the ``gco-hyperpod`` Kueue queue needs no toleration at all,
    because the HyperPod ResourceFlavor injects it (docs/HYPERPOD.md, "A Job
    needs nothing but the queue name" — the fourth #502 release attempt's probe
    Job, exactly that shape plus one GPU, was rejected here for the
    ``nvidia.com/gpu`` taint it would never meet); a Job that selects the nodes
    itself must tolerate ``gco.aws/hyperpod`` or it stays Pending forever, the
    same failure this check exists to catch early.

    For a TrainJob the accelerator request usually lives in
    ``spec.trainer.resourcesPerNode`` while the toleration can only be expressed
    through a ``runtimePatches`` pod spec, so the requested set and the
    tolerations are each unioned across every pod-spec view before matching.

    *policy* is accepted for signature symmetry with the other checks and is
    unused: which taints exist is a property of how GCO builds nodepools, not of
    per-deployment configuration. Whether this check runs at all is the caller's
    decision, gated on ``require_accelerator_toleration``.

    Returns ``(is_valid, error_message)``; the message is ``None`` when valid.
    """
    del policy  # see docstring
    if manifest.get("kind") == "TrainJob":
        pod_specs = trainjob_validation_pod_specs(manifest)
        hint_example = "examples/kubeflow-trainjob.yaml (GPU variant, via runtimePatches)"
    else:
        single = extract_pod_spec(manifest)
        pod_specs = [single] if single else []
        hint_example = "examples/gpu-job.yaml"
    if not pod_specs:
        return True, None

    requested: set[str] = set()
    tolerations: list[dict[str, Any]] = []
    for pod_spec in pod_specs:
        requested.update(requested_accelerators(pod_spec))
        tolerations.extend(pod_spec.get("tolerations", []) or [])

    routing = hyperpod_routing(manifest, pod_specs)
    if routing == "queue":
        return True, None
    if routing == "selector":
        if _toleration_matches(tolerations, HYPERPOD_TAINT_KEY):
            return True, None
        return (
            False,
            f"Job selects SageMaker HyperPod nodes ({CAPACITY_PROVIDER_LABEL}="
            f"{CAPACITY_PROVIDER_HYPERPOD}) but no matching toleration for taint "
            f"{HYPERPOD_TAINT_KEY}=true:NoSchedule was found. add a matching toleration "
            f"(e.g. key '{HYPERPOD_TAINT_KEY}', operator 'Equal', value 'true', effect "
            f"'NoSchedule'), or label the Job {KUEUE_QUEUE_NAME_LABEL}: "
            f"{HYPERPOD_LOCAL_QUEUE} and let Kueue inject it; see docs/HYPERPOD.md",
        )
    if not requested:
        return True, None

    for taint in requested:
        if not _toleration_matches(tolerations, taint):
            hint = (
                f"add a matching toleration (e.g. key '{taint}', operator "
                f"'Exists', effect 'NoSchedule'); see {hint_example}"
            )
            return (
                False,
                f"Job requests accelerator '{taint}' but no matching "
                f"toleration for taint {taint}=true:NoSchedule was found. {hint}",
            )
    return True, None


# ---------------------------------------------------------------------------
# AWS Nitro Sandboxes (docs/NITRO_SANDBOXES.md)
# ---------------------------------------------------------------------------

#: The RuntimeClasses EKS ships for AWS Nitro Sandboxes. On EKS Auto Mode both
#: exist on every cluster that supports the feature (EKS 1.36 or later in a
#: Region that offers it); the RuntimeClass carries the node selector and the
#: per-pod overhead, so the pod spec needs nothing else.
ISOLATED_SANDBOX = "isolated-sandbox"
CONFIDENTIAL_SANDBOX = "confidential-sandbox"
SANDBOX_RUNTIME_CLASSES = (ISOLATED_SANDBOX, CONFIDENTIAL_SANDBOX)
#: The extended resource each sandbox pod consumes one of, through its
#: RuntimeClass overhead (Kueue counts it, so a ClusterQueue must cover it).
SANDBOX_SLOT_RESOURCE = "eks.amazonaws.com/nitro-sandbox"
SANDBOX_ANNOTATION_PREFIX = "sandbox.amazonaws.com/"
#: The Cedar policy annotation a confidential pod must carry, generated by
#: ``eks-nitro-sandbox-genpolicy`` from the final pod spec.
SANDBOX_POLICY_ANNOTATION = "sandbox.amazonaws.com/policy"
#: Allowed kinds that carry one pod template (or are a Pod), so a runtime class
#: and the sandbox annotations have one place to go.
SANDBOX_TEMPLATE_KINDS = (
    "Pod",
    "Job",
    "CronJob",
    "Deployment",
    "StatefulSet",
    "DaemonSet",
)
_SANDBOX_KINDS_TEXT = f"{', '.join(SANDBOX_TEMPLATE_KINDS[:-1])} or {SANDBOX_TEMPLATE_KINDS[-1]}"
#: Allowed kinds that run no pods, which ``--sandbox`` passes through. Together
#: with ``SANDBOX_TEMPLATE_KINDS`` and TrainJob (pods from its runtime, which
#: ``--sandbox`` refuses) these cover ``DEFAULT_ALLOWED_KINDS``; a test holds
#: the three to it so a new kind has to pick one.
PODLESS_KINDS = ("ConfigMap", "Service")
#: ``--sandbox`` values and the runtime class each selects. Isolated only: a
#: confidential pod needs a Cedar policy generated from its final spec, so it
#: arrives as a manifest that already names its runtime class and policy.
SANDBOX_MODES = {"isolated": ISOLATED_SANDBOX}


def pod_template(manifest: dict[str, Any]) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    """``(pod metadata, pod spec)`` for a kind in ``SANDBOX_TEMPLATE_KINDS``.

    Both are live references into the manifest. For a bare Pod the metadata is
    the Pod's own; for a workload it is the pod template's, which is where
    Kubernetes takes pod annotations from. ``(None, None)`` for other kinds or
    a malformed shape.
    """
    kind = manifest.get("kind")
    spec = manifest.get("spec")
    if kind not in SANDBOX_TEMPLATE_KINDS or not isinstance(spec, dict):
        return None, None
    if kind == "Pod":
        metadata = manifest.get("metadata")
        return (metadata if isinstance(metadata, dict) else None), spec
    if kind == "CronJob":
        job_spec = (spec.get("jobTemplate") or {}).get("spec")
        template = job_spec.get("template") if isinstance(job_spec, dict) else None
    else:
        template = spec.get("template")
    if not isinstance(template, dict):
        return None, None
    metadata, pod_spec = template.get("metadata"), template.get("spec")
    return (
        metadata if isinstance(metadata, dict) else None,
        pod_spec if isinstance(pod_spec, dict) else None,
    )


def _sandbox_candidate_pod_specs(manifest: dict[str, Any]) -> list[dict[str, Any]]:
    if manifest.get("kind") == "TrainJob":
        return trainjob_validation_pod_specs(manifest)
    _, pod_spec = pod_template(manifest)
    return [pod_spec] if pod_spec is not None else []


def sandbox_runtime_class(manifest: dict[str, Any]) -> str | None:
    """The Nitro Sandbox runtime class a manifest's pods use, or ``None``."""
    for pod_spec in _sandbox_candidate_pod_specs(manifest):
        runtime_class = pod_spec.get("runtimeClassName")
        if runtime_class in SANDBOX_RUNTIME_CLASSES:
            return str(runtime_class)
    return None


def check_sandbox(manifest: dict[str, Any]) -> tuple[bool, str | None]:
    """Reject a sandboxed workload that could not start in a Nitro Sandbox.

    Each rule is a documented Nitro Sandbox constraint that otherwise surfaces
    late: the pod is accepted and then stays Pending (no sandbox node can take
    a GPU request) or fails to start. Returns ``(is_valid, error_message)``.

    * No GPU, Neuron or EFA request: sandboxes have no device passthrough.
    * No ``hostNetwork: true`` (the sandbox has its own network stack) and no
      ``hostUsers: false`` (user namespaces are not supported on sandbox nodes).
    * ``sandbox.amazonaws.com/`` annotations on the pod template, not on a
      workload's own metadata, where Kubernetes ignores them. A
      ``network-isolated`` annotation in the wrong place would otherwise leave
      the network open without a word.
    * A confidential pod carries its Cedar policy annotation; confidential mode
      denies a pod without one. GCO cannot generate it: the policy pins the
      final pod spec, so ``eks-nitro-sandbox-genpolicy`` must run on the exact
      manifest that is submitted.
    * A confidential pod sets ``automountServiceAccountToken`` itself. GCO
      defaults it to false at submission, and the policy pins the token mount
      (genpolicy reads the field), so a default added after the policy was
      generated would get the pod denied.
    """
    kind = manifest.get("kind")
    sandboxed = [
        spec
        for spec in _sandbox_candidate_pod_specs(manifest)
        if spec.get("runtimeClassName") in SANDBOX_RUNTIME_CLASSES
    ]
    if not sandboxed:
        return True, None
    for pod_spec in sandboxed:
        runtime_class = pod_spec["runtimeClassName"]
        accelerators = requested_accelerators(pod_spec)
        if accelerators:
            return (
                False,
                f"{runtime_class}: Nitro Sandboxes have no GPU or device passthrough, so a "
                f"sandboxed pod cannot request {', '.join(sorted(accelerators))}; run this "
                "workload without a sandbox runtime class",
            )
        if pod_spec.get("hostNetwork") is True:
            return (
                False,
                f"{runtime_class}: a sandboxed pod has its own network stack and cannot set "
                "hostNetwork: true",
            )
        if pod_spec.get("hostUsers") is False:
            return (
                False,
                f"{runtime_class}: hostUsers: false (user namespaces) is not supported on sandbox "
                "nodes; remove it, the sandbox is already the isolation boundary",
            )
    if kind == "TrainJob":
        if any(spec["runtimeClassName"] == CONFIDENTIAL_SANDBOX for spec in sandboxed):
            return (
                False,
                "confidential-sandbox needs a Cedar policy generated from the final pod spec, "
                "which a TrainJob's runtime builds when it runs; submit the workload as a "
                f"{_SANDBOX_KINDS_TEXT}",
            )
        return True, None
    metadata = manifest.get("metadata")
    own = (metadata.get("annotations") or {}) if isinstance(metadata, dict) else {}
    misplaced = sorted(key for key in own if str(key).startswith(SANDBOX_ANNOTATION_PREFIX))
    if kind != "Pod" and misplaced:
        where = (
            "spec.jobTemplate.spec.template.metadata.annotations"
            if kind == "CronJob"
            else "spec.template.metadata.annotations"
        )
        return (
            False,
            f"{', '.join(misplaced)} on the {kind}'s own metadata has no effect; Kubernetes "
            f"takes pod annotations from the pod template, so put it under {where}",
        )
    template_metadata, template_spec = pod_template(manifest)
    if template_spec is not None and template_spec.get("runtimeClassName") == CONFIDENTIAL_SANDBOX:
        annotations = (template_metadata or {}).get("annotations") or {}
        if not annotations.get(SANDBOX_POLICY_ANNOTATION):
            return (
                False,
                f"confidential-sandbox pods need a {SANDBOX_POLICY_ANNOTATION} annotation on "
                "the pod template: generate it with `eks-nitro-sandbox-genpolicy --profile "
                "confidential` from the exact manifest you submit",
            )
        if "automountServiceAccountToken" not in template_spec:
            return (
                False,
                "confidential-sandbox pods must set automountServiceAccountToken (true or "
                "false) before the policy is generated: GCO sets false when it is missing, "
                "and the policy pins the token mount, so the sandbox would deny the pod",
            )
    return True, None


def apply_sandbox_runtime_class(manifest: dict[str, Any], runtime_class: str) -> None:
    """Set ``runtimeClassName`` on a manifest's pods (``gco jobs submit --sandbox``).

    Raises ``ValueError`` for a kind without a pod template GCO can set, or a
    pod template that already names a different runtime class.
    """
    if runtime_class not in SANDBOX_RUNTIME_CLASSES:
        raise ValueError(f"unknown sandbox runtime class {runtime_class!r}")
    kind = manifest.get("kind")
    metadata = manifest.get("metadata")
    name = metadata.get("name", "?") if isinstance(metadata, dict) else "?"
    _, pod_spec = pod_template(manifest)
    if pod_spec is None:
        raise ValueError(
            f"{kind} {name}: --sandbox needs a {_SANDBOX_KINDS_TEXT}, whose pod template GCO "
            "can set"
        )
    existing = pod_spec.get("runtimeClassName")
    if existing not in (None, "", runtime_class):
        raise ValueError(
            f"{kind} {name} already sets runtimeClassName: {existing}; remove it or drop --sandbox"
        )
    pod_spec["runtimeClassName"] = runtime_class


def apply_sandbox(manifests: list[dict[str, Any]], mode: str) -> None:
    """Run every pod a submission creates in a Nitro Sandbox (``--sandbox MODE``).

    ConfigMaps and Services pass through. Everything else fails closed with
    ``ValueError`` rather than letting a pod run outside the sandbox the caller
    asked for: a TrainJob (its pods come from the training runtime), a pod
    template that names another runtime class, or a submission with no pods.
    The result must also pass :func:`check_sandbox`, so a GPU Job fails here,
    before any request, on every transport including ``submit-direct``, which
    skips server-side admission.
    """
    runtime_class = SANDBOX_MODES.get(mode)
    if runtime_class is None:
        raise ValueError(f"unknown --sandbox mode {mode!r}; choose from {', '.join(SANDBOX_MODES)}")
    sandboxed = 0
    for manifest in manifests:
        if manifest.get("kind") in PODLESS_KINDS:
            continue
        apply_sandbox_runtime_class(manifest, runtime_class)
        ok, message = check_sandbox(manifest)
        if not ok:
            raise ValueError(message)
        sandboxed += 1
    if not sandboxed:
        raise ValueError(
            f"--sandbox found nothing to run in a sandbox: none of the manifests is a "
            f"{_SANDBOX_KINDS_TEXT}"
        )


def check_sandbox_runtime_class(
    manifest: dict[str, Any], runtime_class_exists: Callable[[str], bool]
) -> tuple[bool, str | None]:
    """Reject a sandboxed manifest when this cluster lacks its RuntimeClass.

    EKS creates the sandbox RuntimeClasses only where it supports Nitro
    Sandboxes, so their presence is the test, not a list of Regions and
    versions that would go stale. Without it a Job is accepted and its pods are
    then refused at creation, which leaves the Job sitting with no pods.
    ``runtime_class_exists`` asks the cluster.
    """
    runtime_class = sandbox_runtime_class(manifest)
    if runtime_class is None or runtime_class_exists(runtime_class):
        return True, None
    return (
        False,
        f"RuntimeClass {runtime_class} does not exist on this cluster, so its pods would be "
        "refused at creation: AWS Nitro Sandboxes need EKS 1.36 or later in a Region where "
        "EKS offers them (docs/NITRO_SANDBOXES.md)",
    )
