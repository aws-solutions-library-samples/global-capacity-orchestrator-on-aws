"""
Inference Monitor — reconciliation controller for inference endpoints.

Runs in each regional EKS cluster and polls the global DynamoDB table
(gco-inference-endpoints) to reconcile desired state with actual
Kubernetes resources. Follows a GitOps-style reconciliation pattern:

    DynamoDB (desired state) → inference_monitor → Kubernetes (actual state)

The monitor:
- Creates Deployments, Services, and Ingress rules for new endpoints
- Updates existing deployments when spec changes
- Scales deployments up/down
- Tears down resources when endpoints are deleted
- Reports per-region status back to DynamoDB

Environment Variables:
    CLUSTER_NAME: Name of the EKS cluster
    REGION: AWS region this monitor runs in
    INFERENCE_ENDPOINTS_TABLE_NAME: DynamoDB table name
    RECONCILE_INTERVAL_SECONDS: Seconds between reconciliation loops (default: 15)
    INFERENCE_NAMESPACE: Namespace for inference workloads (default: gco-inference)
"""

import asyncio
import base64
import json
import logging
import os
import re
import secrets
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from kubernetes import client, config
from kubernetes.client.models import V1Deployment
from kubernetes.client.rest import ApiException

from gco.services.inference_store import InferenceEndpointStore
from gco.services.structured_logging import configure_structured_logging

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)


class NetworkPolicyApplyError(Exception):
    """An intra-namespace allow rule could not be applied.

    Raised when materialization-time enforcement fails to create or verify one
    of the allow rules that disaggregated inference depends on. The default-deny
    posture is left intact — only the widening allow rule failed — and ``rule``
    names the offending NetworkPolicy so callers can surface exactly which rule
    could not be applied.
    """

    def __init__(self, rule: str, reason: str):
        self.rule = rule
        self.reason = reason
        super().__init__(f"Network policy {rule!r} could not be applied: {reason}")


class AdminApiKeySecretError(Exception):
    """The proxy admin API key Secret is missing or empty.

    Raised before the prefill-decode proxy is materialized when the Secret
    named by ``proxy.admin_api_key_secret`` is absent, names no Secret at all,
    or carries an empty ``ADMIN_API_KEY`` value. The proxy guards a privileged
    admin path, so it is never started without a usable key: no proxy
    Deployment, Service, or Ingress is created. ``secret`` records the Secret
    name that was looked for (or ``None`` when the spec named none) so callers
    can surface exactly what was missing.
    """

    def __init__(self, secret: str | None, reason: str):
        self.secret = secret
        self.reason = reason
        named = repr(secret) if secret else "<unnamed>"
        super().__init__(f"Admin API key Secret {named} is unusable: {reason}")


# Valid TCP port boundaries for KV-transfer bootstrap ports.
MIN_BOOTSTRAP_PORT = 1024
MAX_BOOTSTRAP_PORT = 65535

# vLLM kv_role for each worker role: prefill produces KV, decode consumes it,
# and a single-instance store node both produces and consumes.
_KV_ROLE_BY_WORKER_ROLE = {
    "prefill": "kv_producer",
    "decode": "kv_consumer",
    "single": "kv_both",
}

# The worker roles each mooncake mode supports. Disaggregated and both split
# work across prefill/decode; store runs a single kv_both instance.
_WORKER_ROLES_BY_MODE = {
    "disaggregated": {"prefill", "decode"},
    "store": {"single"},
    "both": {"prefill", "decode"},
}

# The EFA RDMA fabric is advertised as a Kubernetes extended resource, gated by
# a node taint, and selected through a node label. KV cache transfer over
# RoCE only runs on nodes that carry all three.
EFA_RESOURCE_NAME = "vpc.amazonaws.com/efa"
EFA_NODE_SELECTOR_KEY = "efa"
EFA_NODE_SELECTOR_VALUE = "true"

# The shared per-region Mooncake master exposes its RPC service and the
# built-in HTTP metadata server on these fixed ports.
MOONCAKE_MASTER_RPC_PORT = 50051
MOONCAKE_METADATA_PORT = 8080
MOONCAKE_MASTER_SERVICE = "mooncake-master"

# GPU utilization is not a Kubernetes Resource metric, so a native
# HorizontalPodAutoscaler cannot scale on it (Resource metrics are limited to
# cpu and memory). The cluster's amazon-cloudwatch-observability agent publishes
# per-pod GPU utilization to CloudWatch ContainerInsights, so any autoscaler
# that requests a GPU metric is materialized as a KEDA ScaledObject with an
# aws-cloudwatch trigger instead. KEDA generates the backing HPA under the hood,
# and cpu/memory targets ride along as native cpu/memory triggers on the same
# ScaledObject. KEDA is a mandatory cluster component, so this path is always
# available.
KEDA_API_GROUP = "keda.sh"
KEDA_API_VERSION = "v1alpha1"
KEDA_SCALEDOBJECT_PLURAL = "scaledobjects"

# Metric types that can only be served via CloudWatch (KEDA), keyed to the
# ContainerInsights metric the aws-cloudwatch trigger reads. PodName in the
# ContainerInsights dimension set is the workload (Deployment) name, so the
# dimension triple ClusterName/Namespace/PodName yields the average across a
# Deployment's pods — exactly the signal autoscaling needs.
GPU_METRIC_NAMESPACE = "ContainerInsights"
_CLOUDWATCH_METRIC_BY_TYPE = {
    "gpu": "pod_gpu_utilization",
    "gpu_memory": "pod_gpu_memory_utilization",
}

# Default base port for the KV-transfer bootstrap handshake (VLLM_MOONCAKE_
# BOOTSTRAP_PORT) and the span of per-worker ports derived from it. vLLM assigns
# each worker base_port + dp_rank * tp_size + tp_rank, so the intra-namespace
# allow rule opens a contiguous window starting at the base port. A spec may
# override the base via mooncake.transfer.bootstrap_base_port.
MOONCAKE_BOOTSTRAP_BASE_PORT = 8998
MOONCAKE_BOOTSTRAP_PORT_SPAN = 100

# Environment variable through which each role pod receives the KV-transfer
# bootstrap base port. vLLM derives per-worker ports (base + dp_rank * tp_size +
# tp_rank) from it, so prefill and decode agree on the handshake ports.
VLLM_MOONCAKE_BOOTSTRAP_PORT_ENV = "VLLM_MOONCAKE_BOOTSTRAP_PORT"

# Each role pod reads the shared transport settings (metadata-server address,
# protocol, device) from the rendered mooncake.json. The per-endpoint
# ``{name}-mooncake`` ConfigMap is mounted read-only at the directory below, and
# the connector is pointed at the file through MOONCAKE_CONFIG_PATH.
MOONCAKE_CONFIG_PATH_ENV = "MOONCAKE_CONFIG_PATH"
MOONCAKE_CONFIG_MOUNT_DIR = "/etc/mooncake"
MOONCAKE_CONFIG_FILE_PATH = f"{MOONCAKE_CONFIG_MOUNT_DIR}/mooncake.json"

# Label selector identifying inference workload pods (prefill/decode/proxy and
# legacy single-Deployment endpoints). Used as both the target and the peer of
# the intra-namespace allow rules.
INFERENCE_POD_SELECTOR = {"gco.io/type": "inference"}

# Names of the intra-namespace allow rules the monitor maintains alongside the
# default-deny posture in gco-inference. These mirror the manifest names in
# 03-network-policies.yaml so a failure can point at the same object an operator
# would inspect with kubectl.
NETWORK_POLICY_POD_TO_MASTER = "allow-pod-to-master"
NETWORK_POLICY_POD_TO_METADATA = "allow-pod-to-metadata"
NETWORK_POLICY_RDMA_BOOTSTRAP = "allow-rdma-bootstrap"

# Regional configuration keys through which the in-region deployment supplies
# the shared master's address. The store cannot be wired without an own-region
# master address, so a store-bearing endpoint defers configuration when the
# master address is absent or blank. The metadata server defaults to the master
# host on the metadata port when not supplied explicitly.
MOONCAKE_MASTER_ADDRESS_ENV = "MOONCAKE_MASTER_ADDRESS"
MOONCAKE_METADATA_SERVER_ENV = "MOONCAKE_METADATA_SERVER"

# Container image for the shared per-region master. Supplied by the in-region
# deployment so the master tracks the same pinned build the manifests use.
MOONCAKE_MASTER_IMAGE_ENV = "MOONCAKE_MASTER_IMAGE"

# Maximum time a store-bearing endpoint keeps deferring role-pod creation while
# the shared master has not reported a Ready replica. Past this window the
# monitor keeps deferring and stays in the ``creating`` state, but also surfaces
# an error so operators can see the master never came up. The master itself is
# never deleted or modified on account of this timeout.
MOONCAKE_MASTER_READY_TIMEOUT_SECONDS = 600

# Object-key prefix under which cold-tier KV objects are written in the
# general-purpose regional bucket. Mirrors the value the regional stack and the
# `gco inference populate-kv` upload surface use; kept local so the monitor
# needs no infrastructure (CDK) imports at runtime.
MOONCAKE_COLD_TIER_KEY_PREFIX = "mooncake-kv"

# SSM namespace publishing the always-on general-purpose regional bucket's
# discovery values for a region. Mirrors the value the regional stack writes;
# kept local so the monitor needs no infrastructure (CDK) imports at runtime.
REGIONAL_SHARED_SSM_PARAMETER_PREFIX = "/gco/regional-shared-bucket"

# Matches an AWS region identifier embedded in an address (host or URI), e.g.
# ``us-east-1``, ``eu-west-2``, ``ap-southeast-1``, ``us-gov-west-1``. KV
# transfer over RoCE is intra-region, so any address a topology wires to must
# resolve to the monitor's own region; an embedded token naming a different
# region marks the address as out-of-region. An address that carries no token
# (a bare in-cluster Service name) is region-local by construction.
_REGION_TOKEN_PATTERN = re.compile(r"\b[a-z]{2}-(?:gov-)?[a-z]+-\d+\b")

# --- PD proxy behavior -------------------------------------------------------
#
# The prefill-decode proxy that fronts a disaggregated endpoint checks whether a
# prompt's KV blocks already live in the shared store before it sends the prompt
# to a prefill pod. That check is bounded: it is given this many seconds, and a
# miss or a check that does not finish in time is treated as "not resident" so
# the prompt goes to prefill without the request waiting any longer. Holding the
# bound here keeps the proxy responsive even when the store is slow or
# unreachable.
PD_PROXY_RESIDENCY_TIMEOUT_SECONDS = 2

# Default strategy for spreading requests across the backends of a single role.
PD_PROXY_DEFAULT_SCHEDULING = "round_robin"

# Where a residency miss or timed-out lookup is sent. The prompt always goes to
# a prefill pod in that case; the proxy never stalls the request on the store.
PD_PROXY_RESIDENCY_MISS_TARGET = "prefill"

# The residency lookup never blocks the request: a slow or failed store check
# falls through to prefill rather than holding the client.
PD_PROXY_RESIDENCY_BLOCKING = "false"

# Decode-phase requests only ever reach decode pods that report Ready; pods that
# are still starting are skipped.
PD_PROXY_DECODE_ROUTING_READY_ONLY = "ready_only"

# When no decode pod reports Ready, the proxy refuses the request outright
# instead of streaming a partial generation. The refusal carries a stable
# status and message so clients can distinguish "no backend yet" from a model
# error.
PD_PROXY_NO_DECODE_BACKEND_ACTION_REJECT = "reject"
PD_PROXY_NO_DECODE_BACKEND_STATUS = "503"
PD_PROXY_NO_DECODE_BACKEND_MESSAGE = "no available decode backend"

# Environment variable names the proxy container reads to pick up the behavior
# above. Surfacing them here keeps the proxy's runtime contract in one place;
# the reconcile path attaches the values produced by ``build_pd_proxy_config``.
PD_PROXY_RESIDENCY_TIMEOUT_ENV = "PD_PROXY_RESIDENCY_TIMEOUT_SECONDS"
PD_PROXY_RESIDENCY_BLOCKING_ENV = "PD_PROXY_RESIDENCY_CHECK_BLOCKING"
PD_PROXY_RESIDENCY_MISS_TARGET_ENV = "PD_PROXY_RESIDENCY_MISS_TARGET"
PD_PROXY_DECODE_ROUTING_ENV = "PD_PROXY_DECODE_ROUTING"
PD_PROXY_NO_DECODE_BACKEND_ACTION_ENV = "PD_PROXY_NO_DECODE_BACKEND_ACTION"
PD_PROXY_NO_DECODE_BACKEND_STATUS_ENV = "PD_PROXY_NO_DECODE_BACKEND_STATUS"
PD_PROXY_NO_DECODE_BACKEND_MESSAGE_ENV = "PD_PROXY_NO_DECODE_BACKEND_MESSAGE"
PD_PROXY_SCHEDULING_ENV = "PD_PROXY_SCHEDULING"
PD_PROXY_STORE_ADDRESS_ENV = "PD_PROXY_STORE_ADDRESS"

# Marker label carried by proxy pods so a Service can select the proxy alone,
# distinct from the prefill/decode role pods (which carry their own role marker).
PD_PROXY_ROLE_LABEL = "proxy"

# TCP port the proxy container listens on for the public serving paths.
PD_PROXY_PORT = 8000

# Public serving path prefix the proxy fronts. Client traffic to the OpenAI-
# compatible serving paths (``/v1/...``) is routed to the proxy Service; the
# proxy's admin path (``/instances/add``) is deliberately kept off this prefix.
PD_PROXY_PUBLIC_PATH_PREFIX = "/v1"

# The proxy's privileged admin path. It registers and deregisters scaled
# prefill/decode pods and is never published on the public Ingress; only the
# serving prefix above is routed in from outside the namespace.
PD_PROXY_ADMIN_PATH = "/instances/add"

# Environment variable the proxy reads its admin key from, and the data key the
# backing Kubernetes Secret stores it under. The key value is delivered to the
# container through a Secret reference at pod start — it is never written to the
# endpoint spec or passed as a command-line argument.
PD_PROXY_ADMIN_API_KEY_ENV = "ADMIN_API_KEY"
ADMIN_API_KEY_SECRET_DATA_KEY = "ADMIN_API_KEY"


@dataclass
class RegionServicesResolution:
    """Outcome of resolving the in-region service addresses an endpoint needs.

    ``render_mooncake_config`` consumes already-resolved values via a
    ``region_services`` dict; this carries that dict together with the signals
    a reconcile pass acts on:

    - ``region_services`` is the resolved dict to render with, or ``None`` when
      rendering must be skipped.
    - ``render_skipped`` is set when the store is enabled but the own-region
      master address is not configured: the existing endpoint configuration is
      left untouched and ``store_master_unresolved`` records why.
    - ``cold_tier_unresolved`` is set when the cold tier was requested but the
      own-region general-purpose bucket could not be resolved; the cold tier is
      dropped while the hot-path store keeps operating, and ``error`` explains
      the condition.
    """

    region_services: dict[str, Any] | None = None
    render_skipped: bool = False
    store_master_unresolved: bool = False
    cold_tier_unresolved: bool = False
    error: str | None = None


@dataclass
class MasterReadinessGate:
    """Outcome of gating dependent role-pod creation on the shared master.

    A store-bearing endpoint must not materialize its role pods until the
    single shared ``mooncake-master`` reports a Ready replica. This carries the
    decision a reconcile pass acts on:

    - ``proceed`` is ``True`` only when the master reports at least one Ready
      replica; the caller may then create the dependent role pods and advance
      out of ``creating``. While it is ``False`` the caller materializes no
      dependent pods.
    - ``state`` is the endpoint state to report. It is ``"creating"`` whenever
      creation is deferred (master not ready, still within the wait window,
      timed out, or could not be created) and ``None`` when the gate is open.
    - ``error`` records why creation could not advance: the master did not
      become Ready within the wait window, or the master could not be created.
      It is ``None`` while the master is simply still coming up within the
      window, and ``None`` once the gate is open.
    """

    proceed: bool = False
    state: str | None = None
    error: str | None = None


@dataclass
class RegionalScopeResolution:
    """Outcome of confirming a disaggregated topology stays inside one region.

    KV cache transfer over RoCE cannot cross a region boundary, so every
    ``MooncakeConnector`` peer address and the ``master_server_address`` a
    topology wires to must resolve to the monitor's own region. An endpoint
    that targets several regions runs one independent topology per region; each
    region's monitor reconciles only its own topology and confirms that
    topology's addresses never escape the region.

    - ``in_region`` is ``True`` only when every resolved address belongs to the
      monitor's own region. While it is ``True`` the caller may materialize the
      topology's role Deployments.
    - ``peer_addresses`` lists the addresses that were resolved and checked, in
      a stable order, so callers and logs can show exactly what was wired.
    - ``state`` is the endpoint state to report. It is ``"failed"`` when an
      out-of-region address is detected and ``None`` when the topology is
      wholly in-region.
    - ``error`` describes the cross-region boundary violation — which addresses
      resolved to which other regions — when one is found, and is ``None``
      otherwise. When a violation is reported the caller materializes no role
      Deployments and leaves any previously materialized resources unchanged.
    """

    in_region: bool = True
    peer_addresses: list[str] = field(default_factory=list)
    state: str | None = None
    error: str | None = None


def build_kv_transfer_config(mooncake: dict[str, Any], role: str) -> str:
    """Return the JSON string for vLLM's ``--kv-transfer-config``.

    Translates a mooncake spec block plus a worker role into the connector
    configuration vLLM expects:

    - ``disaggregated`` emits a ``MooncakeConnector``.
    - ``store`` emits a ``MooncakeStoreConnector``.
    - ``both`` emits a ``MultiConnector`` wrapping a ``MooncakeConnector``
      (index 0) followed by a ``MooncakeStoreConnector`` (index 1), both
      sharing the role's ``kv_role``.

    The emitted ``kv_role`` is ``kv_producer`` for prefill, ``kv_consumer``
    for decode, and ``kv_both`` for a single store instance.

    No RDMA endpoints are embedded here; transport configuration is supplied
    separately via the file mounted at ``MOONCAKE_CONFIG_PATH``.

    Args:
        mooncake: The ``spec["mooncake"]`` block; its ``mode`` selects the
            connector shape.
        role: One of ``"prefill"``, ``"decode"``, or ``"single"``.

    Returns:
        A JSON object string parseable by vLLM.

    Raises:
        ValueError: If the ``(mode, role)`` combination is not supported. No
            configuration is emitted in that case.
    """
    mode = mooncake.get("mode")
    supported_roles = _WORKER_ROLES_BY_MODE.get(mode) if isinstance(mode, str) else None
    if supported_roles is None or role not in supported_roles:
        raise ValueError(f"Unsupported (mode, role) pair: ({mode!r}, {role!r})")

    kv_role = _KV_ROLE_BY_WORKER_ROLE[role]

    if mode == "disaggregated":
        return json.dumps({"kv_connector": "MooncakeConnector", "kv_role": kv_role})

    if mode == "store":
        return json.dumps({"kv_connector": "MooncakeStoreConnector", "kv_role": kv_role})

    # mode == "both": MultiConnector chains transfer then store.
    return json.dumps(
        {
            "kv_connector": "MultiConnector",
            "kv_role": kv_role,
            "kv_connector_extra_config": {
                "connectors": [
                    {"kv_connector": "MooncakeConnector", "kv_role": kv_role},
                    {"kv_connector": "MooncakeStoreConnector", "kv_role": kv_role},
                ]
            },
        }
    )


def bootstrap_port_for_worker(base_port: int, dp_rank: int, tp_size: int, tp_rank: int) -> int:
    """Compute the bootstrap port for a ``(dp_rank, tp_rank)`` worker.

    The port is ``base_port + dp_rank * tp_size + tp_rank``. For a fixed
    ``base_port`` and ``tp_size`` distinct ``(dp_rank, tp_rank)`` pairs map to
    distinct ports.

    Args:
        base_port: The base bootstrap port for the endpoint.
        dp_rank: The data-parallel rank of the worker (``>= 0``).
        tp_size: The tensor-parallel world size (``>= 1``).
        tp_rank: The tensor-parallel rank within the worker (``0 <= tp_rank < tp_size``).

    Returns:
        The TCP port assigned to the worker.

    Raises:
        ValueError: If the computed port falls outside the valid range
            ``1024..65535``. No port is assigned in that case.
    """
    port = base_port + dp_rank * tp_size + tp_rank
    if port < MIN_BOOTSTRAP_PORT or port > MAX_BOOTSTRAP_PORT:
        raise ValueError(
            f"Computed bootstrap port {port} is outside the valid range "
            f"{MIN_BOOTSTRAP_PORT}..{MAX_BOOTSTRAP_PORT}"
        )
    return port


def render_mooncake_config(
    mooncake: dict[str, Any], region_services: dict[str, Any]
) -> dict[str, Any]:
    """Render the ``mooncake.json`` contents mounted into each vLLM pod.

    The returned dict is written verbatim to a ConfigMap and mounted at the
    path named by ``MOONCAKE_CONFIG_PATH``. It always carries the metadata
    server and the RDMA/TCP transport settings (``protocol`` and
    ``device_name``); the key-value store and its optional cold tier are layered
    on only when requested.

    The transport block (``protocol``/``device_name``) describes the hot
    RDMA/RoCE path. The cold tier is an asynchronous object-store backend keyed
    separately as ``cold_tier_s3_uri``; it is never wired into the transport
    block, so cold-tier reads and writes stay off the RDMA hot path.

    Resolution of in-region addresses is the caller's responsibility: this
    function consumes already-resolved values from ``region_services`` and
    performs no lookups of its own. In particular, the cold-tier URI is the
    monitor-resolved general-purpose regional bucket for the monitor's own
    region; any cold-tier bucket URI in the user spec is ignored.

    Args:
        mooncake: The ``spec["mooncake"]`` block.
        region_services: Resolved in-region addresses, e.g.::

            {
                "metadata_server": "http://mooncake-master:8080/metadata",
                "master_server_address": "mooncake-master:50051",
                "cold_tier_s3_uri": "s3://gco-regional-shared-<acct>-<region>/...",
            }

            ``master_server_address`` is required when the store is enabled and
            ``cold_tier_s3_uri`` is required when the cold tier is enabled.

    Returns:
        The ``mooncake.json`` contents as a dict, where:

        - ``protocol`` and ``device_name`` are always present.
        - ``master_server_address`` is present only when the store is enabled.
        - ``cold_tier_s3_uri`` is present only when the cold tier is enabled,
          which requires ``cold_tier_enabled`` to be the boolean ``True``; any
          other value (absent, null, truthy non-bool) leaves the cold tier off.
    """
    transfer = mooncake.get("transfer", {})
    store = mooncake.get("store", {})
    cfg: dict[str, Any] = {
        "metadata_server": region_services["metadata_server"],
        "protocol": transfer.get("protocol", "rdma"),
        "device_name": transfer.get("device_name", ""),
    }
    if store.get("enabled"):
        cfg["master_server_address"] = region_services["master_server_address"]
        cfg["global_segment_size"] = store.get("global_segment_size", "0")
        cfg["local_buffer_size"] = store.get("local_buffer_size", "2147483648")
        # Only the boolean True enables the cold tier; any other value leaves it
        # off. The URI is resolved by the caller for the monitor's own region —
        # never authored by the user — and is an object-store backend kept off
        # the RDMA transport block above.
        if store.get("cold_tier_enabled") is True:
            cfg["cold_tier_s3_uri"] = region_services["cold_tier_s3_uri"]
    return cfg


def apply_efa_scheduling(mooncake: dict[str, Any], pod_spec: client.V1PodSpec) -> None:
    """Place a role pod on the EFA RDMA fabric when transfer runs over RDMA.

    KV cache transfer over RoCE only runs on EFA-enabled nodes, which carry a
    ``vpc.amazonaws.com/efa`` taint, advertise the ``vpc.amazonaws.com/efa``
    extended resource, and are labelled ``efa=true``. When the transfer
    protocol is ``rdma`` this mutates ``pod_spec`` in place to:

    - add a ``vpc.amazonaws.com/efa`` toleration (in addition to any existing
      tolerations such as the GPU one),
    - add an ``efa=true`` node selector (merged with any existing selectors),
      and
    - request at least one ``vpc.amazonaws.com/efa`` device on the pod's
      containers, leaving every existing resource request and limit — including
      GPU asks — untouched.

    When the transfer protocol is explicitly set to anything other than
    ``rdma`` (for example ``tcp``) the pod is left exactly as it was: no
    toleration, no node selector, and no device request are added. An unset
    protocol defaults to ``rdma`` — matching the rest of the Mooncake path — so
    a disaggregated endpoint lands on EFA by default.

    Tolerations, selectors, and device requests are applied idempotently, so
    re-running over an already-scheduled pod produces no duplicates.

    Args:
        mooncake: The ``spec["mooncake"]`` block; ``transfer.protocol``
            (defaulting to ``rdma`` when unset) decides whether EFA scheduling
            applies.
        pod_spec: The pod specification to mutate in place.
    """
    transfer = mooncake.get("transfer", {})
    if transfer.get("protocol", "rdma") != "rdma":
        return

    # Tolerate the EFA taint without disturbing existing tolerations.
    tolerations = list(pod_spec.tolerations or [])
    if not any(t.key == EFA_RESOURCE_NAME for t in tolerations):
        tolerations.append(
            client.V1Toleration(
                key=EFA_RESOURCE_NAME,
                operator="Equal",
                value="true",
                effect="NoSchedule",
            )
        )
    pod_spec.tolerations = tolerations

    # Merge the EFA node selector with any selectors already in place.
    node_selector = dict(pod_spec.node_selector or {})
    node_selector[EFA_NODE_SELECTOR_KEY] = EFA_NODE_SELECTOR_VALUE
    pod_spec.node_selector = node_selector

    # Request at least one EFA device, preserving existing requests and limits
    # (notably the GPU asks). Apply to containers that already request an
    # accelerator; if none do, apply to every container so the pod still asks
    # for the fabric it needs.
    containers = pod_spec.containers or []
    accelerator_keys = ("nvidia.com/gpu", "aws.amazon.com/neuron")

    def _requests_accelerator(container: client.V1Container) -> bool:
        reqs = container.resources
        if reqs is None:
            return False
        for table in (reqs.requests, reqs.limits):
            if table and any(key in table for key in accelerator_keys):
                return True
        return False

    targets = [c for c in containers if _requests_accelerator(c)] or list(containers)
    for container in targets:
        if container.resources is None:
            container.resources = client.V1ResourceRequirements()
        if container.resources.requests is None:
            container.resources.requests = {}
        if container.resources.limits is None:
            container.resources.limits = {}
        container.resources.requests.setdefault(EFA_RESOURCE_NAME, "1")
        container.resources.limits.setdefault(EFA_RESOURCE_NAME, "1")


def build_pd_proxy_config(mooncake: dict[str, Any]) -> dict[str, str]:
    """Return the environment the prefill-decode proxy runs with.

    The proxy fronts a disaggregated endpoint and decides, per request, whether
    to consult the shared store and which backends to dispatch to. Its behavior
    is fixed by the values returned here so every disaggregated endpoint front
    behaves identically:

    - It looks up whether the prompt's KV blocks already reside in the store
      before sending the prompt to prefill, and that lookup is bounded to
      ``PD_PROXY_RESIDENCY_TIMEOUT_SECONDS`` seconds.
    - A miss, or a lookup that does not finish in time, is treated as "not
      resident": the prompt is sent to a prefill pod and the request is never
      held waiting on the store.
    - Decode-phase requests are routed only to decode pods reporting Ready, so a
      pod that is still starting is skipped.
    - When no decode pod reports Ready, the proxy refuses the request with a
      stable status and message rather than streaming any partial output.

    The residency bound is held constant rather than read from the spec so the
    responsiveness guarantee cannot be weakened per endpoint. The store address
    points at the shared in-region master, and the same-role dispatch strategy
    falls back to round-robin when the spec names none.

    Args:
        mooncake: The ``spec["mooncake"]`` block; its optional ``proxy`` section
            supplies the same-role scheduling strategy.

    Returns:
        A mapping of environment variable name to value, ready to attach to the
        proxy container.
    """
    proxy = mooncake.get("proxy", {}) or {}
    scheduling = proxy.get("scheduling") or PD_PROXY_DEFAULT_SCHEDULING
    store_address = f"{MOONCAKE_MASTER_SERVICE}:{MOONCAKE_MASTER_RPC_PORT}"
    return {
        PD_PROXY_RESIDENCY_TIMEOUT_ENV: str(PD_PROXY_RESIDENCY_TIMEOUT_SECONDS),
        PD_PROXY_RESIDENCY_BLOCKING_ENV: PD_PROXY_RESIDENCY_BLOCKING,
        PD_PROXY_RESIDENCY_MISS_TARGET_ENV: PD_PROXY_RESIDENCY_MISS_TARGET,
        PD_PROXY_DECODE_ROUTING_ENV: PD_PROXY_DECODE_ROUTING_READY_ONLY,
        PD_PROXY_NO_DECODE_BACKEND_ACTION_ENV: PD_PROXY_NO_DECODE_BACKEND_ACTION_REJECT,
        PD_PROXY_NO_DECODE_BACKEND_STATUS_ENV: PD_PROXY_NO_DECODE_BACKEND_STATUS,
        PD_PROXY_NO_DECODE_BACKEND_MESSAGE_ENV: PD_PROXY_NO_DECODE_BACKEND_MESSAGE,
        PD_PROXY_SCHEDULING_ENV: scheduling,
        PD_PROXY_STORE_ADDRESS_ENV: store_address,
    }


class InferenceMonitor:
    """
    Reconciliation controller for inference endpoints.

    Polls DynamoDB for desired endpoint state and reconciles with
    the actual Kubernetes resources in the local cluster.
    """

    def __init__(
        self,
        cluster_id: str,
        region: str,
        store: InferenceEndpointStore,
        namespace: str = "gco-inference",
        reconcile_interval: int = 15,
    ):
        self.cluster_id = cluster_id
        self.region = region
        self.store = store
        self.namespace = namespace
        self.reconcile_interval = reconcile_interval
        self._running = False

        # Initialize Kubernetes clients
        try:
            config.load_incluster_config()
            logger.info("Loaded in-cluster Kubernetes configuration")
        except config.ConfigException:
            try:
                config.load_kube_config()
                logger.info("Loaded local Kubernetes configuration")
            except config.ConfigException as e:
                logger.error("Failed to load Kubernetes configuration: %s", e)
                raise

        self.apps_v1 = client.AppsV1Api()
        self.core_v1 = client.CoreV1Api()
        self.networking_v1 = client.NetworkingV1Api()

        # Timeout for Kubernetes API calls (seconds)
        self._k8s_timeout = int(os.environ.get("K8S_API_TIMEOUT", "30"))

        # Health watchdog: tracks when each endpoint first became unready.
        # If an endpoint stays unready for longer than _ingress_removal_threshold,
        # the watchdog removes its Ingress to protect the shared ALB from
        # having an unhealthy target group (which would make GA mark the
        # entire ALB as unhealthy, blocking all inference in the region).
        self._unready_since: dict[str, datetime] = {}
        self._ingress_removal_threshold = int(
            os.environ.get("INFERENCE_UNHEALTHY_THRESHOLD_SECONDS", "300")
        )  # 5 minutes default

        # Master-readiness gate: tracks when each store-bearing endpoint first
        # deferred its role-pod creation because the shared master was not yet
        # Ready. The entry is cleared once the master reports a Ready replica so
        # a later restart of the master restarts the clock cleanly.
        self._master_deferral_since: dict[str, datetime] = {}

        # Metrics
        self._reconcile_count = 0
        self._errors_count = 0

    # ------------------------------------------------------------------
    # Reconciliation loop
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """Start the reconciliation loop with leader election.

        Uses a Kubernetes Lease object for leader election so that only
        one replica reconciles at a time. Other replicas stay on standby
        and take over if the leader dies.
        """
        if self._running:
            logger.warning("Inference monitor already running")
            return
        self._running = True
        logger.info(
            "Starting inference monitor for %s in %s (interval=%ds)",
            self.cluster_id,
            self.region,
            self.reconcile_interval,
        )

        # Namespace and ServiceAccount are pre-created by the kubectl-applier
        # at deploy time (00-namespaces.yaml, 01-serviceaccounts.yaml). The
        # inference-monitor SA has namespace-scoped RBAC only — it cannot
        # read_namespace/create_namespace, so we don't try. If the namespace
        # is ever missing, deployments below will fail with a clear 404.

        # Get pod identity for leader election
        pod_name = os.environ.get("HOSTNAME", f"monitor-{id(self)}")
        lease_name = "inference-monitor-leader"

        while self._running:
            try:
                if self._try_acquire_lease(lease_name, pod_name):
                    await self.reconcile()
                else:
                    logger.debug("Not the leader, waiting...")
            except Exception as e:
                logger.error("Reconciliation error: %s", e, exc_info=True)
                self._errors_count += 1
            try:
                await asyncio.sleep(self.reconcile_interval)
            except Exception as e:
                logger.error("Sleep interrupted: %s", e)
                break

    def _try_acquire_lease(self, lease_name: str, holder: str) -> bool:
        """Try to acquire or renew a Kubernetes Lease for leader election.

        Uses optimistic concurrency via resourceVersion — if two monitors
        race to update the same lease, K8s returns 409 Conflict for the
        loser, preventing split-brain.

        Returns True if this instance is the leader.
        """

        coordination_v1 = client.CoordinationV1Api()
        now = datetime.now(UTC)

        try:
            lease = coordination_v1.read_namespaced_lease(lease_name, self.namespace)
            current_holder = lease.spec.holder_identity
            renew_time = lease.spec.renew_time

            # Check if lease is expired (holder hasn't renewed in 3x interval)
            if renew_time:
                elapsed = (now - renew_time.replace(tzinfo=UTC)).total_seconds()
                if elapsed > self.reconcile_interval * 3:
                    # Lease expired — take over
                    logger.info("Lease expired (held by %s), taking over", current_holder)
                    current_holder = None

            if current_holder == holder:
                # We're the leader — renew
                lease.spec.renew_time = now
                try:
                    coordination_v1.replace_namespaced_lease(lease_name, self.namespace, lease)
                except ApiException as conflict:
                    if conflict.status == 409:
                        logger.debug("Lease renew conflict (another writer), retrying next cycle")
                        return False
                    raise
                return True
            if current_holder is None or current_holder == "":
                # No leader — claim it
                lease.spec.holder_identity = holder
                lease.spec.renew_time = now
                try:
                    coordination_v1.replace_namespaced_lease(lease_name, self.namespace, lease)
                except ApiException as conflict:
                    if conflict.status == 409:
                        logger.info("Lost lease race to another monitor")
                        return False
                    raise
                logger.info("Acquired leader lease as %s", holder)
                return True
            # Someone else is the leader
            return False

        except ApiException as e:
            if e.status == 404:
                # Lease doesn't exist — create it
                lease = client.V1Lease(
                    metadata=client.V1ObjectMeta(
                        name=lease_name,
                        namespace=self.namespace,
                    ),
                    spec=client.V1LeaseSpec(
                        holder_identity=holder,
                        lease_duration_seconds=self.reconcile_interval * 3,
                        renew_time=now,
                    ),
                )
                try:
                    coordination_v1.create_namespaced_lease(self.namespace, lease)
                    logger.info("Created leader lease as %s", holder)
                    return True
                except ApiException:
                    return False
            logger.warning("Lease check failed: %s", e.reason)
            return False

    def stop(self) -> None:
        """Stop the reconciliation loop."""
        self._running = False
        logger.info("Inference monitor stopped")

    async def reconcile(self) -> list[dict[str, Any]]:
        """
        Run one reconciliation cycle.

        Returns a list of actions taken (for logging/testing).
        """
        self._reconcile_count += 1
        actions: list[dict[str, Any]] = []

        # Get all endpoints from DynamoDB
        try:
            endpoints = self.store.list_endpoints()
        except Exception as e:
            logger.error("Failed to list endpoints from DynamoDB: %s", e)
            return actions

        for endpoint in endpoints:
            try:
                action = await self._reconcile_endpoint(endpoint)
                if action:
                    actions.append(action)
            except Exception as e:
                name = endpoint.get("endpoint_name", "unknown")
                logger.error("Failed to reconcile endpoint %s: %s", name, e)
                self._errors_count += 1
                self.store.update_region_status(
                    name,
                    self.region,
                    "error",
                    error=str(e),
                )

        # Purge fully-deleted endpoints from DynamoDB to prevent unbounded growth.
        # An endpoint is fully deleted when desired_state is "deleted" and all
        # target regions report "deleted" status.
        for endpoint in endpoints:
            if endpoint.get("desired_state") != "deleted":
                continue
            region_status = endpoint.get("region_status", {})
            target_regions = endpoint.get("target_regions", [])
            if not target_regions:
                continue
            all_deleted = all(
                isinstance(region_status.get(r), dict)
                and region_status.get(r, {}).get("state") == "deleted"
                for r in target_regions
            )
            if all_deleted:
                ep_name = endpoint["endpoint_name"]
                try:
                    self.store.delete_endpoint(ep_name)
                    logger.info("Purged fully-deleted endpoint %s from DynamoDB", ep_name)
                    actions.append({"action": "purge", "endpoint": ep_name})
                except Exception as e:
                    logger.warning("Failed to purge endpoint %s: %s", ep_name, e)

        return actions

    async def _reconcile_endpoint(self, endpoint: dict[str, Any]) -> dict[str, Any] | None:
        """Reconcile a single endpoint."""
        name = endpoint["endpoint_name"]
        desired_state = endpoint.get("desired_state", "deploying")
        target_regions = endpoint.get("target_regions", [])
        spec = endpoint.get("spec", {})
        ns = endpoint.get("namespace", self.namespace)

        # Am I a target region?
        if self.region not in target_regions:
            # If I have resources for this endpoint, clean them up
            if self._deployment_exists(name, ns):
                logger.info(
                    "Endpoint %s no longer targets %s, cleaning up",
                    name,
                    self.region,
                )
                self._delete_resources(name, ns)
                self.store.update_region_status(
                    name,
                    self.region,
                    "deleted",
                )
                return {"action": "cleanup", "endpoint": name, "reason": "region_removed"}
            return None

        # Reconcile based on desired state
        if desired_state in ("deploying", "running"):
            return await self._reconcile_running(name, ns, spec, endpoint)
        if desired_state == "stopped":
            return self._reconcile_stopped(name, ns)
        if desired_state == "deleted":
            return self._reconcile_deleted(name, ns)

        return None

    async def _reconcile_running(
        self,
        name: str,
        namespace: str,
        spec: dict[str, Any],
        endpoint: dict[str, Any],
    ) -> dict[str, Any] | None:
        """Ensure the endpoint is running with the correct spec."""
        # Specs carrying a ``mooncake`` block take the disaggregated path. The
        # branch returns ``None`` when no such block is present, so a plain
        # endpoint falls through to the single-Deployment path below unchanged.
        mooncake_action = await self._reconcile_mooncake(name, namespace, spec, endpoint)
        if mooncake_action is not None:
            return mooncake_action

        deployment = self._get_deployment(name, namespace)

        if deployment is None:
            # Create everything
            logger.info("Creating endpoint %s in %s", name, self.region)
            self._create_deployment(name, namespace, spec)
            self._create_service(name, namespace, spec)
            self._update_ingress_rule(name, namespace, spec, endpoint)
            if spec.get("autoscaling", {}).get("enabled"):
                self._create_or_update_hpa(name, namespace, spec)
            self.store.update_region_status(
                name,
                self.region,
                "creating",
                replicas_desired=spec.get("replicas", 1),
            )
            return {"action": "create", "endpoint": name}

        # Deployment exists — ensure Service and Ingress also exist
        # (they may have been manually deleted or lost during a rollout)
        self._ensure_service(name, namespace, spec)

        # Check readiness before ensuring Ingress — the health watchdog may
        # remove the Ingress if the endpoint has been unready too long
        desired_replicas = spec.get("replicas", 1)
        current_replicas = deployment.spec.replicas or 1
        ready_replicas = deployment.status.ready_replicas or 0

        ingress_removed = self._check_health_watchdog(
            name, namespace, ready_replicas, desired_replicas, spec, endpoint
        )
        if not ingress_removed:
            self._ensure_ingress(name, namespace, spec, endpoint)

        if current_replicas != desired_replicas:
            logger.info(
                "Scaling endpoint %s: %d → %d replicas",
                name,
                current_replicas,
                desired_replicas,
            )
            self._scale_deployment(name, namespace, desired_replicas)
            self.store.update_region_status(
                name,
                self.region,
                "updating",
                replicas_ready=ready_replicas,
                replicas_desired=desired_replicas,
            )
            return {"action": "scale", "endpoint": name, "replicas": desired_replicas}

        # Check if image changed
        current_image = self._get_deployment_image(deployment)
        desired_image = self._resolve_image_for_region(spec) if spec.get("image") else ""
        if current_image and desired_image and current_image != desired_image:
            logger.info("Updating endpoint %s image: %s → %s", name, current_image, desired_image)
            self._update_deployment_image(name, namespace, desired_image)
            self.store.update_region_status(
                name,
                self.region,
                "updating",
                replicas_ready=ready_replicas,
                replicas_desired=desired_replicas,
            )
            return {"action": "update_image", "endpoint": name, "image": desired_image}

        # Everything is in sync — report status
        state = "running" if ready_replicas >= desired_replicas else "creating"
        self.store.update_region_status(
            name,
            self.region,
            state,
            replicas_ready=ready_replicas,
            replicas_desired=desired_replicas,
        )

        # Reconcile canary deployment if present
        canary = spec.get("canary")
        if canary:
            self._reconcile_canary(name, namespace, spec, canary, endpoint)
        else:
            # No canary — clean up canary resources if they exist
            self._cleanup_canary(name, namespace)

        # If all replicas are ready and desired_state is "deploying", promote to "running"
        if state == "running" and endpoint.get("desired_state") == "deploying":
            # Check if all target regions are running
            all_running = True
            for r_status in endpoint.get("region_status", {}).values():
                if isinstance(r_status, dict) and r_status.get("state") != "running":
                    all_running = False
                    break
            if all_running:
                self.store.update_desired_state(name, "running")

        return None

    # ------------------------------------------------------------------
    # Mooncake reconciliation branch
    # ------------------------------------------------------------------

    @staticmethod
    def _desired_roles(mode: str | None) -> list[str]:
        """Return the worker roles a mode materializes, in a stable order.

        Disaggregated and ``both`` modes split work across ``prefill`` then
        ``decode``; store mode runs a single ``kv_both`` instance under the
        ``single`` role. The order is fixed so role creation and status
        reporting are deterministic across passes.
        """
        roles = _WORKER_ROLES_BY_MODE.get(mode, set()) if isinstance(mode, str) else set()
        return [role for role in ("prefill", "decode", "single") if role in roles]

    @staticmethod
    def _needs_shared_master(mooncake: dict[str, Any]) -> bool:
        """Whether the endpoint depends on the shared per-region master.

        The store-bearing modes (``store`` and ``both``) always reach the
        master for KV metadata, and any endpoint transferring over RDMA reaches
        the master's built-in metadata server for the connector handshake. A
        disaggregated endpoint transferring over TCP needs no master.
        """
        mode = mooncake.get("mode")
        if mode in ("store", "both"):
            return True
        transfer = mooncake.get("transfer") or {}
        return bool(transfer.get("protocol", "rdma") == "rdma")

    def _ensure_mooncake_configmap(self, name: str, ns: str, cfg: dict[str, Any]) -> None:
        """Create or update the shared transport ConfigMap for an endpoint.

        The rendered transport settings (the dict produced by
        :func:`render_mooncake_config`) are written to a ConfigMap named
        ``{name}-mooncake`` under the ``mooncake.json`` key, which each role pod
        mounts at the configured path. Creation is idempotent: an existing
        ConfigMap is patched to the desired contents so a transport change on
        the spec propagates on the next pass.
        """
        cm_name = f"{name}-mooncake"
        config_map = client.V1ConfigMap(
            metadata=client.V1ObjectMeta(
                name=cm_name,
                namespace=ns,
                labels={"app": name, "project": "gco", "gco.io/type": "inference"},
            ),
            data={"mooncake.json": json.dumps(cfg, sort_keys=True)},
        )
        try:
            self.core_v1.create_namespaced_config_map(
                ns, config_map, _request_timeout=self._k8s_timeout
            )
            logger.info("Created mooncake config map %s/%s", ns, cm_name)
        except ApiException as e:
            if e.status == 409:
                self.core_v1.patch_namespaced_config_map(
                    cm_name, ns, config_map, _request_timeout=self._k8s_timeout
                )
                logger.info("Updated mooncake config map %s/%s", ns, cm_name)
            else:
                raise

    def _ensure_role_deployment(
        self, name: str, ns: str, spec: dict[str, Any], role: str
    ) -> tuple[int, int]:
        """Ensure one role Deployment exists at its desired replica count.

        Creates the role Deployment when absent. When it already exists and an
        autoscaler does not own its count, the replica count is reconciled to
        the topology-desired value so a topology change on the spec takes
        effect. The materialized name is ``{name}`` for the single store role
        and ``{name}-{role}`` for prefill and decode.

        Returns:
            The observed ``(ready, desired)`` replica counts after the pass.
        """
        mooncake = spec.get("mooncake") or {}
        deploy_name = name if role == "single" else f"{name}-{role}"
        desired = self._replica_count_for_role(mooncake, role)

        deployment = self._get_deployment(deploy_name, ns)
        if deployment is None:
            self._create_role_deployment(name, ns, spec, role)
            return 0, desired

        # An autoscaler owns the count for prefill/decode when enabled; leave
        # the running count untouched in that case.
        autoscaling = mooncake.get("autoscaling") or {}
        autoscaled = bool(autoscaling.get("enabled")) and role in ("prefill", "decode")
        current = deployment.spec.replicas or 0
        if not autoscaled and current != desired:
            logger.info(
                "Scaling role deployment %s/%s: %d → %d",
                ns,
                deploy_name,
                current,
                desired,
            )
            self._scale_deployment(deploy_name, ns, desired)

        status = getattr(deployment, "status", None)
        ready = int(getattr(status, "ready_replicas", 0) or 0) if status else 0
        return ready, desired

    def _report_role_status(
        self,
        name: str,
        ns: str,
        mooncake: dict[str, Any],
        region_services: dict[str, Any],
    ) -> str:
        """Write the role-keyed region status for a Mooncake endpoint.

        For split topologies the status carries a ``roles`` map of observed and
        desired replica counts per role; for store-bearing endpoints it carries
        a ``store`` sub-status with the master's readiness and address. The flat
        ``replicas_ready`` / ``replicas_desired`` fields are also populated with
        the totals so consumers that only read the flat shape still see motion.

        Returns:
            The reported endpoint state: ``"running"`` once every desired role
            replica (and, when applicable, the master) is Ready, otherwise
            ``"creating"``.
        """
        mode = mooncake.get("mode")
        roles = self._desired_roles(mode)
        extra: dict[str, Any] = {}
        total_ready = 0
        total_desired = 0
        all_ready = True

        if mode in ("disaggregated", "both"):
            roles_block: dict[str, Any] = {}
            for role in ("prefill", "decode"):
                if role not in roles:
                    continue
                deploy_name = f"{name}-{role}"
                desired = self._replica_count_for_role(mooncake, role)
                dep = self._get_deployment(deploy_name, ns)
                status = getattr(dep, "status", None) if dep else None
                ready = int(getattr(status, "ready_replicas", 0) or 0) if status else 0
                roles_block[role] = {"ready": ready, "desired": desired}
                total_ready += ready
                total_desired += desired
                if ready < desired:
                    all_ready = False
            extra["roles"] = roles_block
        else:
            # Store mode runs a single kv_both Deployment under the endpoint name.
            desired = self._replica_count_for_role(mooncake, "single")
            dep = self._get_deployment(name, ns)
            status = getattr(dep, "status", None) if dep else None
            ready = int(getattr(status, "ready_replicas", 0) or 0) if status else 0
            total_ready += ready
            total_desired += desired
            if ready < desired:
                all_ready = False

        store = mooncake.get("store") or {}
        if store.get("enabled"):
            master_ready = self._mooncake_master_ready_replicas(ns) >= 1
            extra["store"] = {
                "ready": master_ready,
                "master": region_services.get("master_server_address"),
            }
            if not master_ready:
                all_ready = False

        state = "running" if all_ready and total_desired > 0 else "creating"
        self.store.update_region_status(
            name,
            self.region,
            state,
            replicas_ready=total_ready,
            replicas_desired=total_desired,
            extra=extra or None,
        )
        return state

    async def _reconcile_mooncake(
        self,
        name: str,
        ns: str,
        spec: dict[str, Any],
        endpoint: dict[str, Any],
    ) -> dict[str, Any] | None:
        """Reconcile an endpoint whose spec carries a ``mooncake`` block.

        Returns ``None`` when the spec carries no ``mooncake`` block, signalling
        the caller to take the single-Deployment path: one Deployment at the
        configured replica count, one Service, and one Ingress, with no role
        split, proxy, autoscaler, or shared-master dependency. An endpoint that
        looks exactly as it does today therefore flows through unchanged.

        With a ``mooncake`` block present, the topology is materialized in
        dependency order, and the shared ConfigMap and master are laid down
        before any role pod, the roles before the front-end, and the front-end
        before status is written:

        1. Resolve the in-region addresses (master, metadata, optional cold
           tier). When the store is enabled but no own-region master is
           configured, nothing further is materialized; the existing
           configuration is left unchanged and the endpoint is reported as
           still coming up with the unresolved-master reason.
        2. Confirm every wired address stays inside the monitor's own region. A
           cross-region address fails the endpoint and materializes nothing,
           leaving any prior resources in place.
        3. Gate dependent pods on the shared per-region master, which also lays
           down the intra-namespace allow rules. While the master is not Ready,
           or if it could not be created, nothing further is materialized and
           the endpoint is reported as still coming up.
        4. Render and apply the shared transport ConfigMap.
        5. Materialize each role Deployment: prefill and decode for
           disaggregated and both modes, a single ``kv_both`` Deployment for
           store mode.
        6. Materialize each present role's autoscaler when autoscaling is on.
        7. Front disaggregated and both modes with the proxy, its Service, and
           the public Ingress; give store mode a Service and Ingress over its
           single Deployment.
        8. Write the role-keyed region status.

        Returns:
            An action record describing what the pass did, or ``None`` when the
            spec carries no ``mooncake`` block.
        """
        mooncake = spec.get("mooncake")
        if not mooncake:
            return None

        mode = mooncake.get("mode")

        # Step 1: resolve in-region addresses. A store without an own-region
        # master is left untouched and reported as still coming up.
        services = self._resolve_region_services(name, mooncake)
        if services.render_skipped:
            self.store.update_region_status(name, self.region, "creating", error=services.error)
            return {
                "action": "reconcile_mooncake",
                "endpoint": name,
                "deferred": "store_master_unresolved",
            }

        region_services = services.region_services or {}

        # Step 2: keep the topology inside its own region.
        scope = self._resolve_regional_scope(name, ns, spec, region_services)
        if not scope.in_region:
            self.store.update_region_status(
                name, self.region, scope.state or "failed", error=scope.error
            )
            return {
                "action": "reconcile_mooncake",
                "endpoint": name,
                "failed": "cross_region_boundary",
            }

        # Step 3: gate dependent pods on the shared master (and its allow
        # rules). This is also where the master itself is created if absent.
        if self._needs_shared_master(mooncake):
            gate = self._gate_on_mooncake_master(name, ns, spec)
            if not gate.proceed:
                self.store.update_region_status(
                    name, self.region, gate.state or "creating", error=gate.error
                )
                return {
                    "action": "reconcile_mooncake",
                    "endpoint": name,
                    "deferred": "master_not_ready",
                }

        # Step 4: shared transport ConfigMap, applied once before role pods.
        cfg = render_mooncake_config(mooncake, region_services)
        self._ensure_mooncake_configmap(name, ns, cfg)

        # Step 5: role Deployments, in a stable order.
        desired_roles = self._desired_roles(mode)
        for role in desired_roles:
            self._ensure_role_deployment(name, ns, spec, role)

        # Step 6: optional per-role autoscaling.
        if (mooncake.get("autoscaling") or {}).get("enabled"):
            for role in ("prefill", "decode"):
                if role in desired_roles:
                    self._create_role_hpa(name, ns, spec, role)

        # Step 7: front-end. Disaggregated and both run behind the proxy; store
        # exposes its single Deployment directly.
        if mode in ("disaggregated", "both"):
            try:
                self._create_pd_proxy(name, ns, spec, endpoint)
            except AdminApiKeySecretError as e:
                logger.error("Proxy for endpoint %s in %s not started: %s", name, ns, e)
                self.store.update_region_status(name, self.region, "failed", error=str(e))
                return {
                    "action": "reconcile_mooncake",
                    "endpoint": name,
                    "failed": "admin_api_key",
                }
        else:
            self._create_service(name, ns, spec)
            self._ensure_ingress(name, ns, spec, endpoint)

        # Step 8: role-keyed status.
        state = self._report_role_status(name, ns, mooncake, region_services)
        return {"action": "reconcile_mooncake", "endpoint": name, "state": state}

    def _reconcile_stopped(self, name: str, namespace: str) -> dict[str, Any] | None:
        """Scale deployment to zero."""
        deployment = self._get_deployment(name, namespace)
        if deployment is None:
            return None

        current_replicas = deployment.spec.replicas or 0
        if current_replicas > 0:
            logger.info("Stopping endpoint %s (scaling to 0)", name)
            self._scale_deployment(name, namespace, 0)
            self.store.update_region_status(
                name,
                self.region,
                "stopped",
                replicas_ready=0,
                replicas_desired=0,
            )
            return {"action": "stop", "endpoint": name}

        self.store.update_region_status(
            name,
            self.region,
            "stopped",
            replicas_ready=0,
            replicas_desired=0,
        )
        return None

    def _reconcile_deleted(self, name: str, namespace: str) -> dict[str, Any] | None:
        """Delete all resources for the endpoint."""
        # Clean up health watchdog tracker
        self._unready_since.pop(name, None)
        # Clean up the master-readiness deferral tracker
        self._master_deferral_since.pop(name, None)

        # An endpoint is either a single Deployment named ``name`` or a Mooncake
        # role-split topology (``name-prefill``/``name-decode``/``name-proxy``).
        # Check all of them so a disaggregated endpoint is actually torn down
        # rather than skipped (which would orphan its role Deployments — and the
        # GPU nodes they hold).
        deployment_names = (
            name,
            f"{name}-prefill",
            f"{name}-decode",
            f"{name}-proxy",
        )
        if any(self._deployment_exists(d, namespace) for d in deployment_names):
            logger.info("Deleting endpoint %s from %s", name, self.region)
            self._delete_resources(name, namespace)
            self.store.update_region_status(name, self.region, "deleted")
            return {"action": "delete", "endpoint": name}

        self.store.update_region_status(name, self.region, "deleted")
        return None

    # ------------------------------------------------------------------
    # Kubernetes resource management
    # ------------------------------------------------------------------

    def _deployment_exists(self, name: str, namespace: str) -> bool:
        try:
            self.apps_v1.read_namespaced_deployment(
                name, namespace, _request_timeout=self._k8s_timeout
            )
            return True
        except ApiException as e:
            if e.status == 404:
                return False
            raise

    def _get_deployment(self, name: str, namespace: str) -> V1Deployment | None:
        try:
            return self.apps_v1.read_namespaced_deployment(
                name, namespace, _request_timeout=self._k8s_timeout
            )
        except ApiException as e:
            if e.status == 404:
                return None
            raise

    def _get_deployment_image(self, deployment: V1Deployment) -> str | None:
        """Get the image of the first container in a deployment."""
        containers = deployment.spec.template.spec.containers
        if containers:
            image: str = containers[0].image
            return image
        return None

    def _resolve_image_for_region(self, spec: dict[str, Any]) -> str:
        """Pick the image URI this region should pull from.

        ``cli.inference.InferenceManager.deploy`` populates
        ``spec["region_image_uris"]`` with a per-region map when the
        primary image is an ECR URI, so each cluster can pull from its
        local replica instead of crossing the WAN. The map is omitted
        for non-ECR refs and for deploys with ``rewrite_image=False``,
        in which case we fall back to the flat ``spec["image"]`` URI.

        When the map is present but lacks an entry for ``self.region``
        (a target region was added after the spec was last written),
        the flat URI is also used so the deployment doesn't break — the
        next reconcile after a fresh deploy picks up the right URI.
        """
        region_map = spec.get("region_image_uris")
        if isinstance(region_map, dict):
            uri = region_map.get(self.region)
            if isinstance(uri, str) and uri:
                return uri
        return str(spec["image"])

    # ------------------------------------------------------------------
    # In-region service resolution (master address + cold-tier bucket)
    # ------------------------------------------------------------------

    def _resolve_region_services(
        self, name: str, mooncake: dict[str, Any]
    ) -> RegionServicesResolution:
        """Resolve the in-region addresses an endpoint's ``mooncake.json`` needs.

        Everything an endpoint wires to is resolved for the monitor's own
        region from regional configuration — never from values typed into the
        endpoint spec:

        - The shared master's RPC address comes from regional configuration. It
          is required whenever the store is enabled. When the store is enabled
          and no own-region master address is configured, rendering is skipped
          and the existing endpoint configuration is left unchanged; the result
          records the unresolved-master condition so the caller can report it.
        - The metadata server defaults to the master host on the metadata port
          unless regional configuration supplies one explicitly.
        - When the cold tier is opted in (``store.cold_tier_enabled`` is the
          boolean ``True``), the cold-tier object-store URI is resolved to the
          own-region general-purpose regional bucket from that region's
          ``/name`` discovery value. Any cold-tier bucket URI in the spec is
          ignored. Whether the endpoint writes to the cold tier is governed
          solely by the per-endpoint flag, independent of the always-on bucket.
          When the bucket cannot be resolved (the region's stack is not yet
          deployed), the cold tier is dropped and the condition is recorded,
          while the hot-path store stays configured.

        Args:
            name: The endpoint name, used to scope the cold-tier object key.
            mooncake: The ``spec["mooncake"]`` block.

        Returns:
            A :class:`RegionServicesResolution` carrying the resolved
            ``region_services`` dict and any skip/unresolved signals.
        """
        store = mooncake.get("store", {})
        store_enabled = bool(store.get("enabled"))

        master_address = os.environ.get(MOONCAKE_MASTER_ADDRESS_ENV, "").strip()

        # The store needs an own-region master. Without one, leave the endpoint
        # untouched and surface the unresolved-master condition.
        if store_enabled and not master_address:
            return RegionServicesResolution(
                render_skipped=True,
                store_master_unresolved=True,
                error=(
                    "shared store master address is not configured for region "
                    f"{self.region}; deferring store configuration"
                ),
            )

        region_services: dict[str, Any] = {
            "metadata_server": self._metadata_server_url(master_address),
        }
        if store_enabled:
            region_services["master_server_address"] = master_address

        result = RegionServicesResolution(region_services=region_services)

        # Cold tier is opt-in per endpoint. The bucket is always resolved for
        # the monitor's own region; any URI supplied in the spec is ignored.
        if store_enabled and store.get("cold_tier_enabled") is True:
            bucket = self._resolve_regional_shared_bucket()
            if bucket:
                region_services["cold_tier_s3_uri"] = (
                    f"s3://{bucket}/{MOONCAKE_COLD_TIER_KEY_PREFIX}/{name}/"
                )
            else:
                # The own-region general-purpose bucket is not resolvable yet.
                # Drop the cold tier but keep the hot-path store operating.
                result.cold_tier_unresolved = True
                result.error = (
                    "general-purpose regional bucket for region "
                    f"{self.region} could not be resolved; cold tier disabled, "
                    "hot-path store still active"
                )

        return result

    def _metadata_server_url(self, master_address: str) -> str:
        """Return the metadata server URL, deriving it from the master host.

        Regional configuration may supply the metadata server URL directly.
        When it does not, the URL defaults to the master host on the metadata
        port; if no master host is known the conventional in-cluster service
        name is used.
        """
        configured = os.environ.get(MOONCAKE_METADATA_SERVER_ENV, "").strip()
        if configured:
            return configured
        host = master_address.rsplit(":", 1)[0] if master_address else MOONCAKE_MASTER_SERVICE
        return f"http://{host}:{MOONCAKE_METADATA_PORT}/metadata"

    def _resolve_regional_shared_bucket(self) -> str | None:
        """Resolve the own-region general-purpose bucket name, or ``None``.

        Reads the monitor's own region's ``/name`` discovery value for the
        always-on general-purpose regional bucket. Returns ``None`` when the
        value is absent (the region's stack is not yet deployed) or cannot be
        read, so the caller can drop the cold tier without disturbing the
        hot-path store.
        """
        from gco.services.aws_ssm import get_ssm_parameter_optional

        param_name = f"{REGIONAL_SHARED_SSM_PARAMETER_PREFIX}/name"
        try:
            return get_ssm_parameter_optional(param_name, region=self.region)
        except Exception as e:  # noqa: BLE001 - any read failure means "unresolved"
            logger.warning(
                "Failed to resolve general-purpose regional bucket for %s: %s",
                self.region,
                e,
            )
            return None

    # ------------------------------------------------------------------
    # Regional scope boundary (intra-region RDMA enforcement)
    # ------------------------------------------------------------------

    def _region_of_address(self, address: str) -> str:
        """Return the region an address resolves to.

        Addresses carrying an embedded AWS region token (a host or URI such as
        ``mooncake-master.us-east-1.example:50051`` or
        ``http://kv.eu-west-1.internal:8080/metadata``) resolve to that region.
        A bare in-cluster Service name carries no token and is region-local by
        construction, so it resolves to the monitor's own region.

        Args:
            address: A host, ``host:port``, or URI the topology wires to.

        Returns:
            The region the address resolves to: the embedded region token when
            one is present, otherwise the monitor's own region.
        """
        match = _REGION_TOKEN_PATTERN.search(address or "")
        if match:
            return match.group(0)
        return self.region

    def _resolve_regional_scope(
        self,
        name: str,
        ns: str,
        spec: dict[str, Any],
        region_services: dict[str, Any] | None,
    ) -> RegionalScopeResolution:
        """Confirm a disaggregated topology wires only to its own region.

        Gathers every address the own-region topology connects to — the
        ``MooncakeConnector`` peers (the sibling role Services for prefill and
        decode), the shared master's RPC address, and the metadata server — and
        confirms each resolves to the monitor's own region. Any explicitly
        supplied peer addresses in the spec are checked too, so a misconfigured
        endpoint that points a peer or master at another region is caught
        before any role pod is materialized.

        An endpoint that enumerates two or more target regions runs one
        independent topology per region: each region's monitor reconciles only
        its own topology (it reconciles only while ``self.region`` is one of the
        target regions), and this resolution confirms that topology's addresses
        never cross into another region.

        Args:
            name: The endpoint name; used to derive the in-cluster peer Service
                names for the disaggregated roles.
            ns: The namespace the topology materializes into.
            spec: The endpoint spec being reconciled.
            region_services: The resolved in-region addresses (from
                :meth:`_resolve_region_services`), or ``None`` when none were
                resolved. Supplies the master and metadata addresses to check.

        Returns:
            A :class:`RegionalScopeResolution`. When every resolved address is
            own-region, ``in_region`` is ``True`` and the caller may materialize
            the role Deployments. When any address resolves to another region,
            ``in_region`` is ``False``, ``state`` is ``"failed"``, and ``error``
            names the offending addresses; the caller then materializes no role
            Deployments and leaves any prior resources unchanged.
        """
        mooncake = spec.get("mooncake") or {}
        mode = mooncake.get("mode")
        roles = _WORKER_ROLES_BY_MODE.get(mode, set()) if isinstance(mode, str) else set()

        # MooncakeConnector peers are the sibling role Services within this
        # namespace; collect them in a stable order for deterministic reporting.
        addresses: list[str] = []
        if "prefill" in roles or "decode" in roles:
            for role in ("prefill", "decode"):
                addresses.append(f"{name}-{role}.{ns}")

        # The shared master and metadata server the pods reach. These come from
        # the own-region resolution, but are checked here so a foreign address
        # supplied through regional configuration is still caught.
        if region_services:
            master = region_services.get("master_server_address")
            metadata = region_services.get("metadata_server")
            if master:
                addresses.append(str(master))
            if metadata:
                addresses.append(str(metadata))

        # Defensive: honor any explicit peer/master addresses authored on the
        # spec so a hand-edited endpoint cannot smuggle in an out-of-region peer.
        store = mooncake.get("store") or {}
        transfer = mooncake.get("transfer") or {}
        for candidate in (
            store.get("master_server_address"),
            store.get("metadata_server"),
        ):
            if candidate:
                addresses.append(str(candidate))
        explicit_peers = transfer.get("peer_addresses")
        if isinstance(explicit_peers, list):
            addresses.extend(str(peer) for peer in explicit_peers if peer)

        # De-duplicate while preserving first-seen order.
        seen: set[str] = set()
        ordered: list[str] = []
        for address in addresses:
            if address not in seen:
                seen.add(address)
                ordered.append(address)

        out_of_region = [
            (address, self._region_of_address(address))
            for address in ordered
            if self._region_of_address(address) != self.region
        ]

        if out_of_region:
            detail = ", ".join(
                f"{address!r} resolves to region {region}" for address, region in out_of_region
            )
            logger.error(
                "Cross-region boundary violation for endpoint %s in %s: %s",
                name,
                self.region,
                detail,
            )
            return RegionalScopeResolution(
                in_region=False,
                peer_addresses=ordered,
                state="failed",
                error=(f"cross-region boundary violation: {detail}; expected region {self.region}"),
            )

        return RegionalScopeResolution(in_region=True, peer_addresses=ordered)

    def _ensure_mooncake_store(self, ns: str, spec: dict[str, Any]) -> None:
        """Maintain the single shared per-region Mooncake master, idempotently.

        The master is region-shared, not per-endpoint: every endpoint that
        needs the store reaches the same ``mooncake-master`` StatefulSet and the
        headless Service that fronts its RPC and metadata ports. This method
        uses create-if-absent semantics, so any number of calls within a region
        converge on exactly one StatefulSet with a single replica and one
        Service. An already-existing master is left untouched — a conflicting
        create is treated as success and never overwrites the running master.

        The StatefulSet runs the master daemon with its built-in HTTP metadata
        server, exposing RPC on :data:`MOONCAKE_MASTER_RPC_PORT` and the
        metadata endpoint on :data:`MOONCAKE_METADATA_PORT`. Both ports are
        published on the headless Service so in-namespace pods can resolve them.

        Args:
            ns: The namespace the master shares with the inference workloads.
            spec: The endpoint spec being reconciled. Its ``mooncake`` block may
                carry a master image override; otherwise the image is taken from
                the in-region deployment's environment.
        """
        mooncake = spec.get("mooncake", {}) or {}
        store = mooncake.get("store", {}) or {}
        image = store.get("master_image") or os.environ.get(MOONCAKE_MASTER_IMAGE_ENV, "").strip()

        labels = {"app": MOONCAKE_MASTER_SERVICE, "project": "gco"}

        # Headless Service exposing both the RPC and metadata ports. A None
        # cluster IP keeps it headless so the StatefulSet's stable network
        # identity resolves directly.
        service = client.V1Service(
            metadata=client.V1ObjectMeta(
                name=MOONCAKE_MASTER_SERVICE,
                namespace=ns,
                labels=labels,
            ),
            spec=client.V1ServiceSpec(
                cluster_ip="None",
                selector={"app": MOONCAKE_MASTER_SERVICE},
                ports=[
                    client.V1ServicePort(
                        name="rpc",
                        port=MOONCAKE_MASTER_RPC_PORT,
                        target_port="rpc",
                        protocol="TCP",
                    ),
                    client.V1ServicePort(
                        name="metadata",
                        port=MOONCAKE_METADATA_PORT,
                        target_port="metadata",
                        protocol="TCP",
                    ),
                ],
            ),
        )

        container = client.V1Container(
            name=MOONCAKE_MASTER_SERVICE,
            image=image,
            command=["mooncake_master"],
            args=[
                f"--port={MOONCAKE_MASTER_RPC_PORT}",
                "--enable_http_metadata_server=true",
                f"--http_metadata_server_port={MOONCAKE_METADATA_PORT}",
            ],
            ports=[
                client.V1ContainerPort(
                    name="rpc",
                    container_port=MOONCAKE_MASTER_RPC_PORT,
                    protocol="TCP",
                ),
                client.V1ContainerPort(
                    name="metadata",
                    container_port=MOONCAKE_METADATA_PORT,
                    protocol="TCP",
                ),
            ],
            security_context=client.V1SecurityContext(
                allow_privilege_escalation=False,
                # The upstream mooncake_master launcher chmods its bundled
                # binary on startup, which needs a writable root filesystem (a
                # read-only root raised OSError: Read-only file system). The pod
                # also runs as root so the chmod of the root-owned binary is
                # permitted. Privilege escalation stays disabled and all
                # capabilities are dropped, so this is constrained root.
                read_only_root_filesystem=False,
                capabilities=client.V1Capabilities(drop=["ALL"]),
            ),
            resources=client.V1ResourceRequirements(
                requests={"cpu": "250m", "memory": "512Mi"},
                limits={"cpu": "1", "memory": "2Gi"},
            ),
            startup_probe=client.V1Probe(
                tcp_socket=client.V1TCPSocketAction(port="rpc"),
                initial_delay_seconds=5,
                period_seconds=5,
                failure_threshold=30,
            ),
            liveness_probe=client.V1Probe(
                tcp_socket=client.V1TCPSocketAction(port="rpc"),
                initial_delay_seconds=15,
                period_seconds=30,
            ),
            readiness_probe=client.V1Probe(
                # The HTTP metadata server returns 400 for a bare GET /metadata
                # (it expects a ?key=), so an HTTP GET readiness probe never
                # passes. Confirm readiness by checking the metadata port is
                # accepting connections instead.
                tcp_socket=client.V1TCPSocketAction(port="metadata"),
                initial_delay_seconds=10,
                period_seconds=15,
            ),
        )

        stateful_set = client.V1StatefulSet(
            metadata=client.V1ObjectMeta(
                name=MOONCAKE_MASTER_SERVICE,
                namespace=ns,
                labels=labels,
            ),
            spec=client.V1StatefulSetSpec(
                service_name=MOONCAKE_MASTER_SERVICE,
                replicas=1,
                selector=client.V1LabelSelector(match_labels={"app": MOONCAKE_MASTER_SERVICE}),
                template=client.V1PodTemplateSpec(
                    metadata=client.V1ObjectMeta(
                        labels=labels,
                        # Keep Karpenter from consolidating the node out from
                        # under the master: it is a single-replica, stateful
                        # control-plane daemon holding KV metadata for every
                        # in-region endpoint, so an eviction drops that state and
                        # disrupts inference. On lightly-loaded clusters
                        # consolidation otherwise evicts it mid-image-pull before
                        # it can even start.
                        annotations={"karpenter.sh/do-not-disrupt": "true"},
                    ),
                    spec=client.V1PodSpec(
                        service_account_name="gco-service-account",
                        # The upstream mooncake_master launcher chmods its
                        # bundled binary (root-owned in the image) on startup, so
                        # the master must run as root: a non-root uid cannot
                        # chmod a root-owned file ("Operation not permitted").
                        # gco-inference does not enforce restricted Pod Security
                        # and the no-root rule applies only to user-submitted
                        # jobs, so a root platform daemon is consistent here. The
                        # container still drops all Linux capabilities and
                        # disallows privilege escalation (see its securityContext).
                        security_context=client.V1PodSecurityContext(
                            run_as_user=0,
                            run_as_group=0,
                        ),
                        containers=[container],
                        restart_policy="Always",
                    ),
                ),
            ),
        )

        # Create-if-absent: a 409 means the shared master already exists, which
        # is the steady state. Leave it untouched and treat it as success.
        try:
            self.core_v1.create_namespaced_service(ns, service, _request_timeout=self._k8s_timeout)
            logger.info("Created shared mooncake master service in %s", ns)
        except ApiException as e:
            if e.status == 409:
                logger.info("Shared mooncake master service already exists in %s", ns)
            else:
                raise

        try:
            self.apps_v1.create_namespaced_stateful_set(
                ns, stateful_set, _request_timeout=self._k8s_timeout
            )
            logger.info("Created shared mooncake master statefulset in %s", ns)
        except ApiException as e:
            if e.status == 409:
                logger.info("Shared mooncake master statefulset already exists in %s", ns)
            else:
                raise

    def _mooncake_master_ready_replicas(self, ns: str) -> int:
        """Return the shared master's Ready replica count, 0 when absent.

        Reads the ``mooncake-master`` StatefulSet status in ``ns``. A missing
        StatefulSet (404) reports zero Ready replicas rather than raising, so a
        caller gating on readiness simply keeps deferring until it appears.

        Args:
            ns: The namespace the shared master lives in.

        Returns:
            The number of Ready replicas the StatefulSet reports, or 0 when it
            does not yet exist or reports no Ready replicas.
        """
        try:
            status = self.apps_v1.read_namespaced_stateful_set_status(
                MOONCAKE_MASTER_SERVICE, ns, _request_timeout=self._k8s_timeout
            )
        except ApiException as e:
            if e.status == 404:
                return 0
            raise

        ready = getattr(getattr(status, "status", None), "ready_replicas", 0)
        return int(ready or 0)

    def _gate_on_mooncake_master(
        self, name: str, ns: str, spec: dict[str, Any]
    ) -> MasterReadinessGate:
        """Gate dependent role-pod creation on the shared master's readiness.

        Maintains the single shared master (create-if-absent) and then decides
        whether the endpoint's dependent role pods may be materialized:

        - If maintaining the master fails, no dependent pods are materialized,
          any existing master is left unmodified, and the endpoint stays in
          ``creating`` carrying a create-failure error.
        - While the master reports fewer than 1 Ready replica, creation is
          deferred and the endpoint stays in ``creating``. The first deferral
          starts a clock; once it exceeds the wait window the endpoint keeps
          deferring and stays in ``creating`` but also surfaces a not-ready
          error. The master is never deleted or modified on account of the
          timeout.
        - Once the master reports at least 1 Ready replica, the gate opens: the
          clock is cleared and the caller may create the role pods and advance
          out of ``creating``.

        Args:
            name: The endpoint name, used to track its first deferral.
            ns: The namespace the master shares with the workloads.
            spec: The endpoint spec being reconciled.

        Returns:
            A :class:`MasterReadinessGate` describing whether to proceed, the
            endpoint state to report, and any error to surface.
        """
        # Maintain the shared master first. A create failure must not produce
        # any dependent pods and must leave an existing master untouched.
        try:
            self._ensure_mooncake_store(ns, spec)
        except ApiException as e:
            logger.error(
                "Could not create shared mooncake master in %s for endpoint %s: %s",
                ns,
                name,
                e,
            )
            return MasterReadinessGate(
                proceed=False,
                state="creating",
                error="shared master could not be created",
            )

        # Apply the intra-namespace allow rules before any role pod is created.
        # A failure here must fail pod materialization while leaving the
        # default-deny posture intact; surface which rule could not be applied.
        try:
            self._ensure_intra_namespace_network_policies(ns, spec)
        except NetworkPolicyApplyError as e:
            logger.error(
                "Could not apply network policy %s in %s for endpoint %s: %s",
                e.rule,
                ns,
                name,
                e.reason,
            )
            return MasterReadinessGate(
                proceed=False,
                state="creating",
                error=f"network policy {e.rule} could not be applied",
            )

        ready_replicas = self._mooncake_master_ready_replicas(ns)
        if ready_replicas >= 1:
            # Master is Ready: open the gate and reset the deferral clock so a
            # later master restart restarts the wait window cleanly.
            if name in self._master_deferral_since:
                logger.info(
                    "Shared master Ready in %s, resuming creation for endpoint %s",
                    ns,
                    name,
                )
                del self._master_deferral_since[name]
            return MasterReadinessGate(proceed=True, state=None, error=None)

        # Master not Ready: defer creation and report creating. Start the clock
        # on the first deferral.
        now = datetime.now(UTC)
        first_deferral = self._master_deferral_since.setdefault(name, now)
        deferred_for = (now - first_deferral).total_seconds()

        if deferred_for >= MOONCAKE_MASTER_READY_TIMEOUT_SECONDS:
            logger.error(
                "Shared master not Ready in %s after %.0fs, still deferring endpoint %s",
                ns,
                deferred_for,
                name,
            )
            return MasterReadinessGate(
                proceed=False,
                state="creating",
                error="shared master did not become Ready",
            )

        logger.info(
            "Deferring creation for endpoint %s in %s until shared master is Ready",
            name,
            ns,
        )
        return MasterReadinessGate(proceed=False, state="creating", error=None)

    def _ensure_intra_namespace_network_policies(self, ns: str, spec: dict[str, Any]) -> None:
        """Apply the intra-namespace allow rules disaggregated inference needs.

        Alongside the default-deny posture in ``gco-inference`` (defined in
        ``03-network-policies.yaml`` and never touched here), this maintains
        three widening allow rules with create-if-absent semantics:

        - ``allow-pod-to-master`` — inference pods reach the shared master RPC
          port (:data:`MOONCAKE_MASTER_RPC_PORT`).
        - ``allow-pod-to-metadata`` — inference pods reach the shared metadata
          server (:data:`MOONCAKE_METADATA_PORT`).
        - ``allow-rdma-bootstrap`` — inference pods reach each other on the
          contiguous KV-transfer bootstrap port window starting at the spec's
          ``mooncake.transfer.bootstrap_base_port`` (default
          :data:`MOONCAKE_BOOTSTRAP_BASE_PORT`).

        Each rule is created independently; an already-present rule (409) is the
        steady state and counts as success. No deny rule is ever read, modified,
        or deleted, so the default-deny policy is preserved regardless of
        outcome.

        Args:
            ns: The inference namespace the rules apply to.
            spec: The endpoint spec being reconciled; its
                ``mooncake.transfer.bootstrap_base_port`` sizes the bootstrap
                port window.

        Raises:
            NetworkPolicyApplyError: If any single rule cannot be created. The
                error names the failing rule; rules created before the failure
                remain in place and the default-deny policy is untouched.
        """
        transfer = (spec.get("mooncake", {}) or {}).get("transfer", {}) or {}
        base_port = transfer.get("bootstrap_base_port", MOONCAKE_BOOTSTRAP_BASE_PORT)
        try:
            base_port = int(base_port)
        except TypeError, ValueError:
            base_port = MOONCAKE_BOOTSTRAP_BASE_PORT
        end_port = min(base_port + MOONCAKE_BOOTSTRAP_PORT_SPAN, MAX_BOOTSTRAP_PORT)

        labels = {"project": "gco"}
        master_selector = client.V1LabelSelector(match_labels={"app": MOONCAKE_MASTER_SERVICE})
        inference_selector = client.V1LabelSelector(match_labels=INFERENCE_POD_SELECTOR)
        inference_peer = [client.V1NetworkPolicyPeer(pod_selector=inference_selector)]

        policies = [
            (
                NETWORK_POLICY_POD_TO_MASTER,
                client.V1NetworkPolicy(
                    metadata=client.V1ObjectMeta(
                        name=NETWORK_POLICY_POD_TO_MASTER, namespace=ns, labels=labels
                    ),
                    spec=client.V1NetworkPolicySpec(
                        pod_selector=master_selector,
                        policy_types=["Ingress"],
                        ingress=[
                            client.V1NetworkPolicyIngressRule(
                                _from=inference_peer,
                                ports=[
                                    client.V1NetworkPolicyPort(
                                        protocol="TCP", port=MOONCAKE_MASTER_RPC_PORT
                                    )
                                ],
                            )
                        ],
                    ),
                ),
            ),
            (
                NETWORK_POLICY_POD_TO_METADATA,
                client.V1NetworkPolicy(
                    metadata=client.V1ObjectMeta(
                        name=NETWORK_POLICY_POD_TO_METADATA, namespace=ns, labels=labels
                    ),
                    spec=client.V1NetworkPolicySpec(
                        pod_selector=master_selector,
                        policy_types=["Ingress"],
                        ingress=[
                            client.V1NetworkPolicyIngressRule(
                                _from=inference_peer,
                                ports=[
                                    client.V1NetworkPolicyPort(
                                        protocol="TCP", port=MOONCAKE_METADATA_PORT
                                    )
                                ],
                            )
                        ],
                    ),
                ),
            ),
            (
                NETWORK_POLICY_RDMA_BOOTSTRAP,
                client.V1NetworkPolicy(
                    metadata=client.V1ObjectMeta(
                        name=NETWORK_POLICY_RDMA_BOOTSTRAP, namespace=ns, labels=labels
                    ),
                    spec=client.V1NetworkPolicySpec(
                        pod_selector=inference_selector,
                        policy_types=["Ingress"],
                        ingress=[
                            client.V1NetworkPolicyIngressRule(
                                _from=inference_peer,
                                ports=[
                                    client.V1NetworkPolicyPort(
                                        protocol="TCP",
                                        port=base_port,
                                        end_port=end_port,
                                    )
                                ],
                            )
                        ],
                    ),
                ),
            ),
        ]

        for rule_name, policy in policies:
            try:
                self.networking_v1.create_namespaced_network_policy(
                    ns, policy, _request_timeout=self._k8s_timeout
                )
                logger.info("Applied network policy %s in %s", rule_name, ns)
            except ApiException as e:
                if e.status == 409:
                    # Already present — the steady state. Leave it untouched.
                    logger.info("Network policy %s already present in %s", rule_name, ns)
                    continue
                logger.error(
                    "Could not apply network policy %s in %s: %s",
                    rule_name,
                    ns,
                    e,
                )
                raise NetworkPolicyApplyError(rule_name, e.reason or str(e)) from e

    def _create_deployment(self, name: str, namespace: str, spec: dict[str, Any]) -> None:
        """Create a Kubernetes Deployment for an inference endpoint."""
        replicas = spec.get("replicas", 1)
        deployment = self._build_inference_deployment_object(
            name=name,
            deploy_name=name,
            app_label=name,
            namespace=namespace,
            spec=spec,
            replicas=replicas,
        )
        self.apps_v1.create_namespaced_deployment(
            namespace, deployment, _request_timeout=self._k8s_timeout
        )
        logger.info("Created deployment %s/%s", namespace, name)

    def _build_inference_deployment_object(
        self,
        name: str,
        deploy_name: str,
        app_label: str,
        namespace: str,
        spec: dict[str, Any],
        replicas: int,
        extra_args: list[str] | None = None,
        extra_labels: dict[str, str] | None = None,
    ) -> client.V1Deployment:
        """Build the ``V1Deployment`` object for an inference workload.

        Shared by the single-Deployment path and the role-split prefill/decode/
        store paths. ``name`` is the endpoint name (used for the in-cluster
        serving prefix and model cache directory), ``deploy_name`` is the
        Kubernetes object name, and ``app_label`` is the selector label that
        Services and autoscalers target. ``extra_args`` are appended to the
        container args (for example the rendered ``--kv-transfer-config``), and
        ``extra_labels`` are merged into both the Deployment and pod-template
        labels so role pods carry a stable role marker.
        """
        image = self._resolve_image_for_region(spec)
        port = spec.get("port", 8000)
        gpu_count = spec.get("gpu_count", 1)
        health_path = spec.get("health_check_path", "/health")
        env_vars = spec.get("env", {})
        # Stable block hashing across data-parallel ranks: identical prompts
        # must hash identically so shared prefix-cache hits are not lost
        # between pods. The disaggregated serving image (upstream vLLM) no
        # longer bakes this in, so default it here; an explicit spec env wins.
        env_vars = {"PYTHONHASHSEED": "0", **env_vars}
        resources = spec.get("resources", {})
        model_path = spec.get("model_path")
        command = spec.get("command")
        args = spec.get("args")

        # Build container
        container_env = [client.V1EnvVar(name=k, value=str(v)) for k, v in env_vars.items()]

        # Inject --root-path for servers that support it (vLLM, TGI).
        # This tells the server to mount its API at /inference/{name}.
        # We append to existing args (from --extra-args) rather than replacing them.
        ingress_prefix = f"/inference/{name}"
        root_path_images = ("vllm", "text-generation-inference", "tgi")
        image_lower = image.lower()
        if not command and any(tag in image_lower for tag in root_path_images):
            if args:
                # Append --root-path to user-provided args if not already present
                if "--root-path" not in args:
                    args = list(args) + ["--root-path", ingress_prefix]
            else:
                args = ["--root-path", ingress_prefix]

        # Append caller-supplied arguments (for example the rendered
        # --kv-transfer-config) after any root-path injection so they survive
        # alongside user --extra-args.
        if extra_args:
            args = (list(args) if args else []) + list(extra_args)

        resource_reqs = client.V1ResourceRequirements(
            requests=resources.get("requests", {"cpu": "1", "memory": "4Gi"}),
            limits=resources.get("limits", {"cpu": "4", "memory": "16Gi"}),
        )
        # Add accelerator resources (GPU or Neuron)
        accelerator = spec.get("accelerator", "nvidia")
        if gpu_count > 0:
            if accelerator == "neuron":
                # AWS Trainium/Inferentia — request Neuron devices
                if resource_reqs.limits is None:
                    resource_reqs.limits = {}
                resource_reqs.limits["aws.amazon.com/neuron"] = str(gpu_count)
                if resource_reqs.requests is None:
                    resource_reqs.requests = {}
                resource_reqs.requests["aws.amazon.com/neuron"] = str(gpu_count)
            else:
                # NVIDIA GPU (default)
                if resource_reqs.limits is None:
                    resource_reqs.limits = {}
                resource_reqs.limits["nvidia.com/gpu"] = str(gpu_count)
                if resource_reqs.requests is None:
                    resource_reqs.requests = {}
                resource_reqs.requests["nvidia.com/gpu"] = str(gpu_count)

        volume_mounts = []
        volumes = []
        init_containers = []
        model_source = spec.get("model_source")

        if model_path or model_source:
            volume_mounts.append(
                client.V1VolumeMount(
                    name="model-storage",
                    mount_path="/models",
                )
            )
            volumes.append(
                client.V1Volume(
                    name="model-storage",
                    persistent_volume_claim=client.V1PersistentVolumeClaimVolumeSource(
                        claim_name="efs-claim",
                    ),
                )
            )

        # Mooncake role pods read the shared transport config (metadata-server
        # address, protocol, device) from the per-endpoint ``{name}-mooncake``
        # ConfigMap mounted read-only at MOONCAKE_CONFIG_MOUNT_DIR, pointed at by
        # MOONCAKE_CONFIG_PATH, and learn the KV-transfer bootstrap base port via
        # VLLM_MOONCAKE_BOOTSTRAP_PORT. Plain (non-mooncake) endpoints are
        # untouched: no volume, mount, or env is added.
        mooncake_block = spec.get("mooncake")
        if mooncake_block:
            volume_mounts.append(
                client.V1VolumeMount(
                    name="mooncake-config",
                    mount_path=MOONCAKE_CONFIG_MOUNT_DIR,
                    read_only=True,
                )
            )
            volumes.append(
                client.V1Volume(
                    name="mooncake-config",
                    config_map=client.V1ConfigMapVolumeSource(name=f"{name}-mooncake"),
                )
            )
            transfer_block = mooncake_block.get("transfer") or {}
            base_port = transfer_block.get("bootstrap_base_port", MOONCAKE_BOOTSTRAP_BASE_PORT)
            try:
                base_port = int(base_port)
            except TypeError, ValueError:
                base_port = MOONCAKE_BOOTSTRAP_BASE_PORT
            container_env.append(
                client.V1EnvVar(name=MOONCAKE_CONFIG_PATH_ENV, value=MOONCAKE_CONFIG_FILE_PATH)
            )
            container_env.append(
                client.V1EnvVar(name=VLLM_MOONCAKE_BOOTSTRAP_PORT_ENV, value=str(base_port))
            )

        # Add init container to sync model from S3 if model_source is set
        if model_source and model_source.startswith("s3://"):
            model_dest = f"/models/{name}"
            init_containers.append(
                client.V1Container(
                    name="model-sync",
                    image="amazon/aws-cli:latest",
                    command=["sh", "-c"],
                    args=[
                        f"if [ -d '{model_dest}' ] && [ \"$(ls -A '{model_dest}')\" ]; then "
                        f"echo 'Model already cached at {model_dest}, skipping sync'; "
                        f"else echo 'Syncing model from {model_source}...'; "
                        f"aws s3 sync {model_source} {model_dest} --quiet; "
                        f"echo 'Model sync complete'; fi"
                    ],
                    volume_mounts=[
                        client.V1VolumeMount(
                            name="model-storage",
                            mount_path="/models",
                        )
                    ],
                    resources=client.V1ResourceRequirements(
                        requests={"cpu": "1", "memory": "2Gi"},
                        limits={"cpu": "4", "memory": "8Gi"},
                    ),
                )
            )

        # Probe path depends on whether the server handles the prefix
        uses_root_path = args is not None and "--root-path" in args
        probe_health = f"{ingress_prefix}{health_path}" if uses_root_path else health_path

        container = client.V1Container(
            name="inference",
            image=image,
            ports=[client.V1ContainerPort(container_port=port)],
            env=container_env if container_env else None,
            resources=resource_reqs,
            volume_mounts=volume_mounts if volume_mounts else None,
            command=command,
            args=args,
            liveness_probe=client.V1Probe(
                http_get=client.V1HTTPGetAction(path=probe_health, port=port),
                initial_delay_seconds=120,
                period_seconds=15,
                failure_threshold=5,
            ),
            readiness_probe=client.V1Probe(
                http_get=client.V1HTTPGetAction(path=probe_health, port=port),
                initial_delay_seconds=30,
                period_seconds=10,
            ),
        )

        # Build tolerations based on accelerator type
        if accelerator == "neuron":
            tolerations = [
                client.V1Toleration(
                    key="aws.amazon.com/neuron",
                    operator="Equal",
                    value="true",
                    effect="NoSchedule",
                )
            ]
        else:
            tolerations = [
                client.V1Toleration(
                    key="nvidia.com/gpu",
                    operator="Equal",
                    value="true",
                    effect="NoSchedule",
                )
            ]

        # Node selector based on accelerator type
        node_selector = spec.get("node_selector", {})
        if gpu_count > 0 and not node_selector:
            if accelerator == "neuron":
                node_selector = {"accelerator": "neuron"}
            else:
                node_selector = {"eks.amazonaws.com/instance-gpu-manufacturer": "nvidia"}

        # Apply capacity type preference (spot/on-demand)
        capacity_type = spec.get("capacity_type")
        if capacity_type in ("spot", "on-demand"):
            node_selector["karpenter.sh/capacity-type"] = capacity_type

        labels = {
            "app": app_label,
            "project": "gco",
            "gco.io/type": "inference",
        }
        if extra_labels:
            labels.update(extra_labels)

        deployment = client.V1Deployment(
            metadata=client.V1ObjectMeta(
                name=deploy_name,
                namespace=namespace,
                labels=dict(labels),
            ),
            spec=client.V1DeploymentSpec(
                replicas=replicas,
                selector=client.V1LabelSelector(
                    match_labels={"app": app_label},
                ),
                template=client.V1PodTemplateSpec(
                    metadata=client.V1ObjectMeta(
                        labels=dict(labels),
                    ),
                    spec=client.V1PodSpec(
                        service_account_name="gco-service-account",
                        containers=[container],
                        init_containers=init_containers if init_containers else None,
                        tolerations=tolerations,
                        node_selector=node_selector if node_selector else None,
                        volumes=volumes if volumes else None,
                    ),
                ),
            ),
        )

        return deployment

    def _replica_count_for_role(self, mooncake: dict[str, Any], role: str) -> int:
        """Resolve the materialized replica count for a role.

        When per-role autoscaling is enabled and supplies a ``min_replicas``
        for the role, the role Deployment is materialized at that lower bound so
        the autoscaler owns the count from there. Otherwise the count comes from
        the topology: ``topology.prefill`` for prefill and ``topology.decode``
        for decode. The single store instance is always one replica.
        """
        autoscaling = mooncake.get("autoscaling") or {}
        if autoscaling.get("enabled") and role in ("prefill", "decode"):
            role_cfg = autoscaling.get(role) or {}
            min_replicas = role_cfg.get("min_replicas")
            if isinstance(min_replicas, int) and not isinstance(min_replicas, bool):
                return min_replicas

        topology = mooncake.get("topology") or {}
        if role == "prefill":
            return int(topology.get("prefill", 1))
        if role == "decode":
            return int(topology.get("decode", 1))
        # Single store instance: kv_both runs as one replica.
        return 1

    def _create_role_deployment(self, name: str, ns: str, spec: dict[str, Any], role: str) -> None:
        """Materialize one role Deployment for a Mooncake endpoint.

        Disaggregated and ``both`` modes split work across ``{name}-prefill``
        and ``{name}-decode``; store mode runs a single ``{name}`` instance with
        the ``kv_both`` role. The role's ``--kv-transfer-config`` is attached to
        the vLLM container, EFA scheduling is applied when transfer runs over
        RDMA, and the replica count is taken from the topology (or the
        autoscaling lower bound when that is enabled).

        Args:
            name: The endpoint name.
            ns: The namespace to materialize into.
            spec: The endpoint spec; ``spec["mooncake"]`` selects the mode and
                topology.
            role: One of ``"prefill"``, ``"decode"``, or ``"single"``.
        """
        mooncake = spec.get("mooncake") or {}

        # The store's single instance keeps the endpoint name; prefill and decode
        # are suffixed so Services and autoscalers can target each role.
        deploy_name = name if role == "single" else f"{name}-{role}"

        kv_transfer_config = build_kv_transfer_config(mooncake, role)
        replicas = self._replica_count_for_role(mooncake, role)

        deployment = self._build_inference_deployment_object(
            name=name,
            deploy_name=deploy_name,
            app_label=deploy_name,
            namespace=ns,
            spec=spec,
            replicas=replicas,
            extra_args=["--kv-transfer-config", kv_transfer_config],
            extra_labels={"gco.io/role": role},
        )

        # Land role pods on the EFA fabric when KV transfer runs over RDMA,
        # preserving the GPU asks already built into the pod.
        apply_efa_scheduling(mooncake, deployment.spec.template.spec)

        self.apps_v1.create_namespaced_deployment(
            ns, deployment, _request_timeout=self._k8s_timeout
        )
        logger.info("Created role deployment %s/%s (role=%s)", ns, deploy_name, role)

    def _verify_admin_api_key_secret(self, proxy: dict[str, Any], ns: str) -> str:
        """Confirm the proxy admin key Secret exists and carries a key value.

        The proxy guards a privileged admin path and must never run without a
        usable ``ADMIN_API_KEY``. This reads the Secret named by
        ``proxy.admin_api_key_secret`` and confirms it holds a non-empty
        ``ADMIN_API_KEY`` value, so the value itself never has to be carried on
        the spec or a command argument.

        Args:
            proxy: The ``spec["mooncake"]["proxy"]`` block; ``admin_api_key_secret``
                names the backing Secret.
            ns: The namespace the Secret lives in.

        Returns:
            The verified Secret name, suitable for a Secret reference.

        Raises:
            AdminApiKeySecretError: If the spec names no Secret, the Secret is
                absent, or its ``ADMIN_API_KEY`` value is empty or missing.
        """
        secret_name = proxy.get("admin_api_key_secret")
        if not isinstance(secret_name, str) or not secret_name:
            raise AdminApiKeySecretError(None, "no admin API key Secret was named")

        try:
            secret = self.core_v1.read_namespaced_secret(
                secret_name, ns, _request_timeout=self._k8s_timeout
            )
        except ApiException as e:
            if e.status == 404:
                raise AdminApiKeySecretError(secret_name, "Secret not found") from e
            raise

        if not self._secret_has_admin_api_key(secret):
            raise AdminApiKeySecretError(
                secret_name,
                f"{ADMIN_API_KEY_SECRET_DATA_KEY} value is empty or missing",
            )

        return secret_name

    @staticmethod
    def _secret_has_admin_api_key(secret: client.V1Secret) -> bool:
        """Return whether ``secret`` carries a non-empty ``ADMIN_API_KEY``.

        Both the base64 ``data`` and the plaintext ``string_data`` views are
        considered, and a value is treated as present only when it decodes to a
        non-empty string.
        """
        string_data = secret.string_data or {}
        plain = string_data.get(ADMIN_API_KEY_SECRET_DATA_KEY)
        if plain:
            return True

        data = secret.data or {}
        encoded = data.get(ADMIN_API_KEY_SECRET_DATA_KEY)
        if not encoded:
            return False
        try:
            return bool(base64.b64decode(encoded))
        except ValueError, TypeError:
            # A value that cannot be decoded is unusable as an admin key.
            return False

    def _ensure_admin_api_key_secret(self, name: str, proxy: dict[str, Any], ns: str) -> str:
        """Return the proxy admin-key Secret name, provisioning one if needed.

        The prefill-decode proxy guards a privileged admin path and must never
        run without a usable ``ADMIN_API_KEY``. Two paths satisfy that:

        - **Bring-your-own**: when the proxy block names a Secret, that Secret
          must already exist and carry a non-empty ``ADMIN_API_KEY``; otherwise
          the deployment is rejected, so a typo or a missing pre-created Secret
          fails fast. The named Secret is only read, never created or mutated.
        - **Auto-managed**: when the proxy names no Secret, a per-endpoint
          ``{name}-admin`` Secret is provisioned create-if-absent with a
          generated key, so a split deploy needs no manual Secret. The generated
          key only ever lives in the cluster — it is never written to the
          endpoint spec, a command argument, or a log line.

        Args:
            name: The endpoint name, used to derive the auto-managed Secret name.
            proxy: The ``spec["mooncake"]["proxy"]`` block.
            ns: The namespace the Secret lives in.

        Returns:
            The Secret name to reference from the proxy container.

        Raises:
            AdminApiKeySecretError: Only on the bring-your-own path, when the
                named Secret is absent or its ``ADMIN_API_KEY`` is empty. The
                auto-managed path never raises this.
        """
        named = proxy.get("admin_api_key_secret")
        if isinstance(named, str) and named:
            return self._verify_admin_api_key_secret(proxy, ns)
        return self._provision_admin_api_key_secret(f"{name}-admin", ns)

    def _provision_admin_api_key_secret(self, secret_name: str, ns: str) -> str:
        """Create the auto-managed proxy admin-key Secret if absent.

        Uses create-if-absent semantics so the key stays stable across reconcile
        passes: an existing Secret (the steady state, or one a prior pass
        created) is left untouched, and a concurrent create (409) is treated as
        success. A freshly created Secret carries a cryptographically strong
        64-character hex ``ADMIN_API_KEY`` from :func:`secrets.token_hex`, which
        reaches the proxy only through a Secret reference — the value is never
        logged or written to the spec.

        Args:
            secret_name: The Secret to ensure exists (``{endpoint}-admin``).
            ns: The namespace to create it in.

        Returns:
            The Secret name, ready for a Secret reference.
        """
        try:
            self.core_v1.read_namespaced_secret(secret_name, ns, _request_timeout=self._k8s_timeout)
            # Already present: keep the existing key so proxy pods need no churn.
            return secret_name
        except ApiException as e:
            if e.status != 404:
                raise

        secret = client.V1Secret(
            metadata=client.V1ObjectMeta(
                name=secret_name,
                namespace=ns,
                labels={"app": secret_name, "project": "gco", "gco.io/type": "inference"},
            ),
            string_data={ADMIN_API_KEY_SECRET_DATA_KEY: secrets.token_hex(32)},
            type="Opaque",
        )
        # The two logger.info calls below carry a bare `# nosemgrep`: the
        # logger-credential-disclosure rule matches the literal word "Secret" in
        # the message, but only the Secret's name and namespace (%s/%s) are
        # logged here — never the generated key value set above in string_data.
        try:
            self.core_v1.create_namespaced_secret(ns, secret, _request_timeout=self._k8s_timeout)
            logger.info("Provisioned proxy admin-key Secret %s/%s", ns, secret_name)  # nosemgrep
        except ApiException as e:
            if e.status == 409:
                logger.info("Proxy admin-key Secret %s/%s exists", ns, secret_name)  # nosemgrep
            else:
                raise
        return secret_name

    def _create_pd_proxy(
        self, name: str, ns: str, spec: dict[str, Any], endpoint: dict[str, Any]
    ) -> None:
        """Materialize the prefill-decode proxy front for a disaggregated endpoint.

        Disaggregated and ``both`` modes are fronted by a lightweight proxy that
        runs the residency check and dispatches each request to the prefill and
        decode pods. This creates three objects, all keyed off ``{name}-proxy``:

        - a proxy Deployment with at least one replica, running the proxy image
          named by ``mooncake.proxy.image`` and carrying the environment from
          :func:`build_pd_proxy_config`;
        - a Service whose selector matches only the proxy pods (the
          ``{name}-proxy`` app label plus the proxy role marker), so it never
          fans out to prefill or decode pods; and
        - an Ingress that routes the public ``/v1/*`` serving paths to that
          Service.

        Before any of those are created, the admin key Secret named by
        ``mooncake.proxy.admin_api_key_secret`` is checked: it must exist and
        carry a non-empty ``ADMIN_API_KEY`` value. The proxy guards a
        privileged admin path, so when the Secret is absent, names no Secret,
        or holds an empty key, no proxy resource is created and the deployment
        is rejected. The key itself reaches the container only as a Secret
        reference at pod start — it is never written to the spec or a command
        argument.

        Creation is idempotent at the API boundary: an already-present Deployment
        or Service is left in place, and an already-present Ingress is patched to
        the desired routing.

        Args:
            name: The endpoint name.
            ns: The namespace to materialize into.
            spec: The endpoint spec; ``spec["mooncake"]`` supplies the proxy
                image and behavior.
            endpoint: The endpoint record, used for any ingress overrides.

        Raises:
            AdminApiKeySecretError: If the admin key Secret is missing, names no
                Secret, or holds an empty ``ADMIN_API_KEY``. No proxy resource
                is created in that case.
        """
        mooncake = spec.get("mooncake") or {}
        proxy = mooncake.get("proxy") or {}
        proxy_name = f"{name}-proxy"

        # The proxy fronts a privileged admin path, so it never starts without a
        # usable admin key. When the spec names a Secret it must already exist
        # and be non-empty (the deployment is rejected otherwise); when it names
        # none, a per-endpoint admin-key Secret is auto-provisioned with a
        # generated key. Either way the key reaches the container only by Secret
        # reference.
        admin_secret_name = self._ensure_admin_api_key_secret(name, proxy, ns)

        proxy_env = build_pd_proxy_config(mooncake)
        container_env = [client.V1EnvVar(name=k, value=v) for k, v in proxy_env.items()]
        # Deliver the admin key by Secret reference only — its value is never
        # placed on the spec or a command argument.
        container_env.append(
            client.V1EnvVar(
                name=PD_PROXY_ADMIN_API_KEY_ENV,
                value_from=client.V1EnvVarSource(
                    secret_key_ref=client.V1SecretKeySelector(
                        name=admin_secret_name,
                        key=ADMIN_API_KEY_SECRET_DATA_KEY,
                    )
                ),
            )
        )

        # The proxy fronts at least one replica; a spec may ask for more.
        replicas = proxy.get("replicas", 1)
        if not isinstance(replicas, int) or isinstance(replicas, bool) or replicas < 1:
            replicas = 1

        labels = {
            "app": proxy_name,
            "project": "gco",
            "gco.io/type": "inference",
            "gco.io/role": PD_PROXY_ROLE_LABEL,
        }

        container = client.V1Container(
            name="proxy",
            image=proxy.get("image"),
            ports=[client.V1ContainerPort(container_port=PD_PROXY_PORT)],
            env=container_env if container_env else None,
            resources=client.V1ResourceRequirements(
                requests={"cpu": "250m", "memory": "256Mi"},
                limits={"cpu": "1", "memory": "1Gi"},
            ),
            readiness_probe=client.V1Probe(
                tcp_socket=client.V1TCPSocketAction(port=PD_PROXY_PORT),
                initial_delay_seconds=10,
                period_seconds=10,
            ),
            liveness_probe=client.V1Probe(
                tcp_socket=client.V1TCPSocketAction(port=PD_PROXY_PORT),
                initial_delay_seconds=30,
                period_seconds=15,
                failure_threshold=5,
            ),
        )

        deployment = client.V1Deployment(
            metadata=client.V1ObjectMeta(
                name=proxy_name,
                namespace=ns,
                labels=dict(labels),
            ),
            spec=client.V1DeploymentSpec(
                replicas=replicas,
                selector=client.V1LabelSelector(match_labels={"app": proxy_name}),
                template=client.V1PodTemplateSpec(
                    metadata=client.V1ObjectMeta(labels=dict(labels)),
                    spec=client.V1PodSpec(
                        service_account_name="gco-service-account",
                        containers=[container],
                    ),
                ),
            ),
        )

        try:
            self.apps_v1.create_namespaced_deployment(
                ns, deployment, _request_timeout=self._k8s_timeout
            )
            logger.info("Created proxy deployment %s/%s", ns, proxy_name)
        except ApiException as e:
            if e.status == 409:
                logger.info("Proxy deployment %s/%s already exists", ns, proxy_name)
            else:
                raise

        self._create_proxy_service(proxy_name, ns)
        self._update_proxy_ingress(name, proxy_name, ns, endpoint)

    def _create_proxy_service(self, proxy_name: str, namespace: str) -> None:
        """Create the Service that fronts only the proxy pods.

        The selector is the ``{name}-proxy`` app label together with the proxy
        role marker, so the Service resolves exclusively to proxy pods and never
        to the prefill or decode role pods that share the namespace.
        """
        service = client.V1Service(
            metadata=client.V1ObjectMeta(
                name=proxy_name,
                namespace=namespace,
                labels={
                    "app": proxy_name,
                    "project": "gco",
                    "gco.io/type": "inference",
                    "gco.io/role": PD_PROXY_ROLE_LABEL,
                },
            ),
            spec=client.V1ServiceSpec(
                selector={"app": proxy_name, "gco.io/role": PD_PROXY_ROLE_LABEL},
                ports=[
                    client.V1ServicePort(
                        port=80,
                        target_port=PD_PROXY_PORT,
                        protocol="TCP",
                    )
                ],
                type="ClusterIP",
            ),
        )

        try:
            self.core_v1.create_namespaced_service(
                namespace, service, _request_timeout=self._k8s_timeout
            )
            logger.info("Created proxy service %s/%s", namespace, proxy_name)
        except ApiException as e:
            if e.status == 409:
                logger.info("Proxy service %s/%s already exists", namespace, proxy_name)
            else:
                raise

    def _update_proxy_ingress(
        self, name: str, proxy_name: str, namespace: str, endpoint: dict[str, Any]
    ) -> None:
        """Create or update the public Ingress that routes the proxy's serving paths.

        Only the OpenAI-compatible serving paths are published, and they are
        scoped to the endpoint's own ingress prefix: the rule routes
        ``{ingress_path}/v1`` (for example ``/inference/{name}/v1``) to the
        proxy Service. Scoping to the endpoint prefix is what makes a
        disaggregated endpoint reachable — every client request arrives at
        ``/inference/{name}/...`` (through Global Accelerator and the shared
        ALB), exactly as it does for a single-Deployment endpoint, so a bare
        ``/v1`` rule would neither match the client URL nor stay isolated from
        other endpoints sharing the ALB. The proxy's privileged admin path
        (``{ingress_path}/instances/add``) is deliberately not among the
        published paths, so an admin request arriving from outside the namespace
        never matches an Ingress rule and is never forwarded to the proxy. The
        Ingress merges onto the shared ALB through the ``alb`` ingress class,
        matching the legacy single-Deployment Ingress convention.
        """
        # The public Ingress carries only the serving prefix, scoped to this
        # endpoint's ingress path so it matches the client URL
        # (``/inference/{name}/v1/...``) and stays isolated from other endpoints
        # on the shared ALB. The proxy's admin path is filtered out so no future
        # edit to the published set can route it in from outside the namespace.
        ingress_path = endpoint.get("ingress_path", f"/inference/{name}")
        serving_prefix = f"{ingress_path}{PD_PROXY_PUBLIC_PATH_PREFIX}"
        admin_prefix = f"{ingress_path}{PD_PROXY_ADMIN_PATH}"
        published_paths = [p for p in [serving_prefix] if p != admin_prefix]
        ingress = client.V1Ingress(
            metadata=client.V1ObjectMeta(
                name=f"inference-{proxy_name}",
                namespace=namespace,
                labels={
                    "app": proxy_name,
                    "project": "gco",
                    "gco.io/type": "inference",
                    "gco.io/role": PD_PROXY_ROLE_LABEL,
                },
                annotations={
                    "alb.ingress.kubernetes.io/healthcheck-path": serving_prefix,
                    "alb.ingress.kubernetes.io/healthcheck-interval-seconds": "15",
                },
            ),
            spec=client.V1IngressSpec(
                ingress_class_name="alb",
                rules=[
                    client.V1IngressRule(
                        http=client.V1HTTPIngressRuleValue(
                            paths=[
                                client.V1HTTPIngressPath(
                                    path=published_path,
                                    path_type="Prefix",
                                    backend=client.V1IngressBackend(
                                        service=client.V1IngressServiceBackend(
                                            name=proxy_name,
                                            port=client.V1ServiceBackendPort(number=80),
                                        ),
                                    ),
                                )
                                for published_path in published_paths
                            ]
                        )
                    )
                ],
            ),
        )

        try:
            self.networking_v1.create_namespaced_ingress(
                namespace, ingress, _request_timeout=self._k8s_timeout
            )
            logger.info(
                "Created proxy ingress for %s routing %s to %s",
                name,
                serving_prefix,
                proxy_name,
            )
        except ApiException as e:
            if e.status == 409:
                self.networking_v1.patch_namespaced_ingress(
                    f"inference-{proxy_name}",
                    namespace,
                    ingress,
                    _request_timeout=self._k8s_timeout,
                )
                logger.info("Updated proxy ingress for %s", name)
            else:
                raise

    def _create_service(self, name: str, namespace: str, spec: dict[str, Any]) -> None:
        """Create a Kubernetes Service for an inference endpoint."""
        port = spec.get("port", 8000)

        service = client.V1Service(
            metadata=client.V1ObjectMeta(
                name=name,
                namespace=namespace,
                labels={
                    "app": name,
                    "project": "gco",
                    "gco.io/type": "inference",
                },
            ),
            spec=client.V1ServiceSpec(
                selector={"app": name},
                ports=[
                    client.V1ServicePort(
                        port=80,
                        target_port=port,
                        protocol="TCP",
                    )
                ],
                type="ClusterIP",
            ),
        )

        try:
            self.core_v1.create_namespaced_service(
                namespace, service, _request_timeout=self._k8s_timeout
            )
            logger.info("Created service %s/%s", namespace, name)
        except ApiException as e:
            if e.status == 409:
                logger.info("Service %s/%s already exists", namespace, name)
            else:
                raise

    def _ensure_service(self, name: str, namespace: str, spec: dict[str, Any]) -> None:
        """Ensure the Service exists, recreating it if missing."""
        try:
            self.core_v1.read_namespaced_service(
                name, namespace, _request_timeout=self._k8s_timeout
            )
        except ApiException as e:
            if e.status == 404:
                logger.warning("Service %s/%s missing, recreating", namespace, name)
                self._create_service(name, namespace, spec)
            else:
                raise

    def _ensure_ingress(
        self,
        name: str,
        namespace: str,
        spec: dict[str, Any],
        endpoint: dict[str, Any],
    ) -> None:
        """Ensure the Ingress exists, recreating it if missing."""
        try:
            self.networking_v1.read_namespaced_ingress(
                f"inference-{name}", namespace, _request_timeout=self._k8s_timeout
            )
        except ApiException as e:
            if e.status == 404:
                logger.warning("Ingress for %s missing, recreating", name)
                self._update_ingress_rule(name, namespace, spec, endpoint)
            else:
                raise

    def _update_ingress_rule(
        self,
        name: str,
        namespace: str,
        spec: dict[str, Any],
        endpoint: dict[str, Any],
    ) -> None:
        """Create or update an Ingress for the inference endpoint.

        The Ingress is created in the same namespace as the Service and pods.
        IngressClassParams with group.name merges all Ingresses onto a single
        shared ALB regardless of namespace.
        """
        ingress_path = endpoint.get("ingress_path", f"/inference/{name}")
        image = spec.get("image", "")
        image_lower = image.lower()
        root_path_images = ("vllm", "text-generation-inference", "tgi")
        uses_root_path = any(tag in image_lower for tag in root_path_images)
        base_health = spec.get("health_check_path", "/health")
        health_path = f"/inference/{name}{base_health}" if uses_root_path else base_health

        ingress = client.V1Ingress(
            metadata=client.V1ObjectMeta(
                name=f"inference-{name}",
                namespace=namespace,
                labels={
                    "app": name,
                    "project": "gco",
                    "gco.io/type": "inference",
                },
                annotations={
                    "alb.ingress.kubernetes.io/healthcheck-path": health_path,
                    "alb.ingress.kubernetes.io/healthcheck-interval-seconds": "15",
                },
            ),
            spec=client.V1IngressSpec(
                ingress_class_name="alb",
                rules=[
                    client.V1IngressRule(
                        http=client.V1HTTPIngressRuleValue(
                            paths=[
                                client.V1HTTPIngressPath(
                                    path=ingress_path,
                                    path_type="Prefix",
                                    backend=client.V1IngressBackend(
                                        service=client.V1IngressServiceBackend(
                                            name=name,
                                            port=client.V1ServiceBackendPort(
                                                number=80,
                                            ),
                                        ),
                                    ),
                                )
                            ]
                        )
                    )
                ],
            ),
        )

        try:
            self.networking_v1.create_namespaced_ingress(
                namespace, ingress, _request_timeout=self._k8s_timeout
            )
            logger.info("Created ingress for %s at %s", name, ingress_path)
        except ApiException as e:
            if e.status == 409:
                self.networking_v1.patch_namespaced_ingress(
                    f"inference-{name}", namespace, ingress, _request_timeout=self._k8s_timeout
                )
                logger.info("Updated ingress for %s", name)
            else:
                raise

    def _check_health_watchdog(
        self,
        name: str,
        namespace: str,
        ready_replicas: int,
        desired_replicas: int,
        spec: dict[str, Any],
        endpoint: dict[str, Any],
    ) -> bool:
        """Health watchdog: remove Ingress for persistently unhealthy endpoints.

        If an endpoint has zero ready replicas for longer than the configured
        threshold, the watchdog removes its Ingress to protect the shared ALB.
        Global Accelerator considers an ALB unhealthy if ANY target group has
        zero healthy targets, so one bad endpoint can block all inference
        traffic to the region.

        When the endpoint recovers (ready_replicas > 0), the Ingress is
        automatically re-created by _ensure_ingress on the next cycle.

        Returns:
            True if the Ingress was removed (caller should skip _ensure_ingress).
            False if the endpoint is healthy or still within the grace period.
        """
        if ready_replicas > 0:
            # Endpoint is healthy — clear the tracker
            if name in self._unready_since:
                logger.info(
                    "Endpoint %s recovered, re-enabling Ingress",
                    name,
                )
                del self._unready_since[name]
            return False

        # Endpoint has zero ready replicas
        now = datetime.now(UTC)

        if name not in self._unready_since:
            # First time seeing this endpoint as unready — start the clock
            self._unready_since[name] = now
            logger.warning(
                "Endpoint %s has 0/%d ready replicas, starting health watchdog timer",
                name,
                desired_replicas,
            )
            return False

        # Check how long it's been unready
        unready_duration = (now - self._unready_since[name]).total_seconds()

        if unready_duration < self._ingress_removal_threshold:
            remaining = self._ingress_removal_threshold - unready_duration
            logger.warning(
                "Endpoint %s unready for %ds (removing Ingress in %ds)",
                name,
                int(unready_duration),
                int(remaining),
            )
            return False

        # Threshold exceeded — remove the Ingress to protect the ALB
        ingress_name = f"inference-{name}"
        try:
            self.networking_v1.delete_namespaced_ingress(
                ingress_name, namespace, _request_timeout=self._k8s_timeout
            )
            logger.warning(
                "WATCHDOG: Removed Ingress for unhealthy endpoint %s "
                "(unready for %ds > %ds threshold). "
                "Ingress will be re-created when the endpoint recovers.",
                name,
                int(unready_duration),
                self._ingress_removal_threshold,
            )
        except ApiException as e:
            if e.status == 404:
                logger.debug("Ingress for %s already removed", name)
            else:
                logger.error("Failed to remove Ingress for %s: %s", name, e)

        return True

    def _scale_deployment(self, name: str, namespace: str, replicas: int) -> None:
        """Scale a deployment to the desired replica count."""
        self.apps_v1.patch_namespaced_deployment(
            name,
            namespace,
            body={"spec": {"replicas": replicas}},
            _request_timeout=self._k8s_timeout,
        )

    def _update_deployment_image(self, name: str, namespace: str, image: str) -> None:
        """Update the container image of a deployment."""
        self.apps_v1.patch_namespaced_deployment(
            name,
            namespace,
            body={
                "spec": {
                    "template": {"spec": {"containers": [{"name": "inference", "image": image}]}}
                }
            },
            _request_timeout=self._k8s_timeout,
        )

    def _reconcile_canary(
        self,
        name: str,
        namespace: str,
        spec: dict[str, Any],
        canary: dict[str, Any],
        endpoint: dict[str, Any],
    ) -> None:
        """Reconcile canary deployment and weighted ingress routing.

        Creates a canary deployment and service alongside the primary,
        then updates the ingress to use ALB action-based weighted routing.
        """
        canary_name = f"{name}-canary"
        canary_image = canary.get("image", "")
        canary_replicas = canary.get("replicas", 1)
        canary_weight = canary.get("weight", 10)
        primary_weight = 100 - canary_weight

        # Build canary spec (same as primary but with canary image/replicas)
        canary_spec = dict(spec)
        canary_spec["image"] = canary_image
        canary_spec["replicas"] = canary_replicas
        # Remove canary field from the canary spec to avoid recursion
        canary_spec.pop("canary", None)

        # Create or update canary deployment
        canary_deployment = self._get_deployment(canary_name, namespace)
        if canary_deployment is None:
            logger.info("Creating canary deployment %s with image %s", canary_name, canary_image)
            self._create_deployment(canary_name, namespace, canary_spec)
            self._create_service(canary_name, namespace, canary_spec)
        else:
            # Update image if changed
            current_image = self._get_deployment_image(canary_deployment)
            if current_image != canary_image:
                self._update_deployment_image(canary_name, namespace, canary_image)
            # Update replicas if changed
            if (canary_deployment.spec.replicas or 1) != canary_replicas:
                self._scale_deployment(canary_name, namespace, canary_replicas)

        # Update ingress with weighted routing via ALB actions annotation
        self._update_canary_ingress(name, namespace, spec, endpoint, primary_weight, canary_weight)

    def _update_canary_ingress(
        self,
        name: str,
        namespace: str,
        spec: dict[str, Any],
        endpoint: dict[str, Any],
        primary_weight: int,
        canary_weight: int,
    ) -> None:
        """Update ingress with ALB weighted target group routing."""
        import json as _json

        ingress_path = endpoint.get("ingress_path", f"/inference/{name}")
        image = spec.get("image", "")
        image_lower = image.lower()
        root_path_images = ("vllm", "text-generation-inference", "tgi")
        uses_root_path = any(tag in image_lower for tag in root_path_images)
        base_health = spec.get("health_check_path", "/health")
        health_path = f"/inference/{name}{base_health}" if uses_root_path else base_health

        # ALB weighted routing via forward action annotation
        forward_config = _json.dumps(
            {
                "type": "forward",
                "forwardConfig": {
                    "targetGroups": [
                        {
                            "serviceName": name,
                            "servicePort": 80,
                            "weight": primary_weight,
                        },
                        {
                            "serviceName": f"{name}-canary",
                            "servicePort": 80,
                            "weight": canary_weight,
                        },
                    ]
                },
            }
        )

        ingress = client.V1Ingress(
            metadata=client.V1ObjectMeta(
                name=f"inference-{name}",
                namespace=namespace,
                labels={
                    "app": name,
                    "project": "gco",
                    "gco.io/type": "inference",
                    "gco.io/canary": "true",
                },
                annotations={
                    "alb.ingress.kubernetes.io/healthcheck-path": health_path,
                    "alb.ingress.kubernetes.io/healthcheck-interval-seconds": "15",
                    "alb.ingress.kubernetes.io/actions.weighted-routing": forward_config,
                },
            ),
            spec=client.V1IngressSpec(
                ingress_class_name="alb",
                rules=[
                    client.V1IngressRule(
                        http=client.V1HTTPIngressRuleValue(
                            paths=[
                                client.V1HTTPIngressPath(
                                    path=ingress_path,
                                    path_type="Prefix",
                                    backend=client.V1IngressBackend(
                                        service=client.V1IngressServiceBackend(
                                            name="weighted-routing",
                                            port=client.V1ServiceBackendPort(
                                                name="use-annotation",
                                            ),
                                        ),
                                    ),
                                )
                            ]
                        )
                    )
                ],
            ),
        )

        try:
            self.networking_v1.patch_namespaced_ingress(
                f"inference-{name}", namespace, ingress, _request_timeout=self._k8s_timeout
            )
            logger.info(
                "Updated ingress for %s: primary=%d%% canary=%d%%",
                name,
                primary_weight,
                canary_weight,
            )
        except ApiException as e:
            if e.status == 404:
                self.networking_v1.create_namespaced_ingress(
                    namespace, ingress, _request_timeout=self._k8s_timeout
                )
                logger.info("Created canary ingress for %s", name)
            else:
                raise

    def _cleanup_canary(self, name: str, namespace: str) -> None:
        """Remove canary deployment, service, and restore primary-only ingress."""
        canary_name = f"{name}-canary"

        # Delete canary deployment
        try:
            self.apps_v1.delete_namespaced_deployment(
                canary_name, namespace, _request_timeout=self._k8s_timeout
            )
            logger.info("Deleted canary deployment %s", canary_name)
        except ApiException as e:
            if e.status != 404:
                logger.error("Failed to delete canary deployment %s: %s", canary_name, e)

        # Delete canary service
        try:
            self.core_v1.delete_namespaced_service(
                canary_name, namespace, _request_timeout=self._k8s_timeout
            )
            logger.info("Deleted canary service %s", canary_name)
        except ApiException as e:
            if e.status != 404:
                logger.error("Failed to delete canary service %s: %s", canary_name, e)

    def _delete_resources(self, name: str, namespace: str) -> None:
        """Delete all Kubernetes resources for an endpoint.

        Covers both the single-Deployment endpoint and the Mooncake role-split
        topology — the ``name-prefill``/``name-decode`` workers, the
        ``name-proxy`` PD proxy, the per-role HPAs, and the ``name-mooncake``
        transport ConfigMap — so deleting a disaggregated endpoint does not
        leave orphaned Deployments (and the GPU nodes they hold) behind. Each
        delete is idempotent: a 404 means that object is not used by this
        endpoint's mode and is ignored. The shared per-region ``mooncake-master``
        is deliberately NOT deleted here, since it is shared across endpoints.
        """
        # Delete canary resources first
        self._cleanup_canary(name, namespace)

        proxy_name = f"{name}-proxy"

        # Deployments: the single-instance endpoint plus the Mooncake prefill/
        # decode workers and the PD proxy.
        for deployment_name in (name, f"{name}-prefill", f"{name}-decode", proxy_name):
            try:
                self.apps_v1.delete_namespaced_deployment(
                    deployment_name, namespace, _request_timeout=self._k8s_timeout
                )
                logger.info("Deleted deployment %s/%s", namespace, deployment_name)
            except ApiException as e:
                if e.status != 404:
                    logger.error("Failed to delete deployment %s: %s", deployment_name, e)

        # Services: the single-instance endpoint Service and the PD proxy Service.
        for service_name in (name, proxy_name):
            try:
                self.core_v1.delete_namespaced_service(
                    service_name, namespace, _request_timeout=self._k8s_timeout
                )
                logger.info("Deleted service %s/%s", namespace, service_name)
            except ApiException as e:
                if e.status != 404:
                    logger.error("Failed to delete service %s: %s", service_name, e)

        # Ingresses: the single-instance endpoint Ingress and the PD proxy Ingress.
        for ingress_name in (f"inference-{name}", f"inference-{proxy_name}"):
            try:
                self.networking_v1.delete_namespaced_ingress(
                    ingress_name, namespace, _request_timeout=self._k8s_timeout
                )
                logger.info("Deleted ingress %s/%s", namespace, ingress_name)
            except ApiException as e:
                if e.status != 404:
                    logger.error("Failed to delete ingress %s: %s", ingress_name, e)

        # HPAs: the single-instance endpoint HPA and the per-role Mooncake HPAs.
        autoscaling_v2 = client.AutoscalingV2Api()
        for hpa_name in (name, f"{name}-prefill", f"{name}-decode"):
            try:
                autoscaling_v2.delete_namespaced_horizontal_pod_autoscaler(hpa_name, namespace)
                logger.info("Deleted HPA %s/%s", namespace, hpa_name)
            except ApiException as e:
                if e.status != 404:
                    logger.error("Failed to delete HPA %s: %s", hpa_name, e)

        # Mooncake transport ConfigMap (a no-op 404 for non-Mooncake endpoints).
        # The shared per-region mooncake-master is intentionally left in place.
        try:
            self.core_v1.delete_namespaced_config_map(
                f"{name}-mooncake", namespace, _request_timeout=self._k8s_timeout
            )
            logger.info("Deleted configmap %s/%s-mooncake", namespace, name)
        except ApiException as e:
            if e.status != 404:
                logger.error("Failed to delete configmap %s-mooncake: %s", name, e)

        # Delete KEDA ScaledObject (the GPU-autoscaling path materializes one of
        # these in place of a native HPA). Best-effort: absence is the common
        # case for cpu/memory-only endpoints.
        try:
            client.CustomObjectsApi().delete_namespaced_custom_object(
                group=KEDA_API_GROUP,
                version=KEDA_API_VERSION,
                namespace=namespace,
                plural=KEDA_SCALEDOBJECT_PLURAL,
                name=name,
                _request_timeout=self._k8s_timeout,
            )
            logger.info("Deleted KEDA ScaledObject for %s", name)
        except ApiException as e:
            if e.status != 404:
                logger.error("Failed to delete KEDA ScaledObject for %s: %s", name, e)

    def _build_hpa_metrics(self, metrics_config: list[dict[str, Any]]) -> list[Any]:
        """Translate a metrics config list into autoscaler metric specs.

        Each entry names a resource (``cpu`` or ``memory``) and a target
        average utilization. Unrecognized entries are skipped, and when nothing
        recognizable remains the autoscaler falls back to scaling on CPU at 70%
        so a Deployment is never left without a scaling signal.
        """
        hpa_metrics = []
        for m in metrics_config:
            metric_type = m.get("type", "cpu")
            target_value = m.get("target", 70)

            if metric_type == "cpu":
                hpa_metrics.append(
                    client.V2MetricSpec(
                        type="Resource",
                        resource=client.V2ResourceMetricSource(
                            name="cpu",
                            target=client.V2MetricTarget(
                                type="Utilization",
                                average_utilization=target_value,
                            ),
                        ),
                    )
                )
            elif metric_type == "memory":
                hpa_metrics.append(
                    client.V2MetricSpec(
                        type="Resource",
                        resource=client.V2ResourceMetricSource(
                            name="memory",
                            target=client.V2MetricTarget(
                                type="Utilization",
                                average_utilization=target_value,
                            ),
                        ),
                    )
                )

        if not hpa_metrics:
            # Default to CPU if no recognized metrics
            hpa_metrics.append(
                client.V2MetricSpec(
                    type="Resource",
                    resource=client.V2ResourceMetricSource(
                        name="cpu",
                        target=client.V2MetricTarget(
                            type="Utilization",
                            average_utilization=70,
                        ),
                    ),
                )
            )

        return hpa_metrics

    @staticmethod
    def _metrics_require_keda(metrics_config: list[dict[str, Any]]) -> bool:
        """Return True when any metric can only be scaled via KEDA/CloudWatch.

        GPU metrics are not Kubernetes Resource metrics, so a native HPA cannot
        consume them. Their presence forces the whole autoscaler onto the KEDA
        ScaledObject path, where cpu/memory targets become native KEDA triggers
        alongside the aws-cloudwatch GPU trigger.
        """
        return any(m.get("type") in _CLOUDWATCH_METRIC_BY_TYPE for m in metrics_config)

    def _build_keda_triggers(
        self,
        metrics_config: list[dict[str, Any]],
        target_name: str,
        namespace: str,
    ) -> list[dict[str, Any]]:
        """Translate a metrics config list into KEDA ScaledObject triggers.

        ``cpu`` and ``memory`` map to KEDA's native resource triggers (the same
        utilization signal a plain HPA would use). ``gpu``/``gpu_memory`` map to
        an ``aws-cloudwatch`` trigger reading the matching ContainerInsights
        metric for this Deployment, identified by the
        ClusterName/Namespace/PodName dimension triple. Unrecognized entries are
        skipped; when nothing recognizable remains the autoscaler falls back to
        CPU at 70% so a Deployment is never left without a scaling signal.
        """
        triggers: list[dict[str, Any]] = []
        for m in metrics_config:
            metric_type = m.get("type", "cpu")
            target_value = m.get("target", 70)

            if metric_type in ("cpu", "memory"):
                triggers.append(
                    {
                        "type": metric_type,
                        "metricType": "Utilization",
                        "metadata": {"value": str(target_value)},
                    }
                )
            elif metric_type in _CLOUDWATCH_METRIC_BY_TYPE:
                triggers.append(
                    {
                        "type": "aws-cloudwatch",
                        "metadata": {
                            "namespace": GPU_METRIC_NAMESPACE,
                            "metricName": _CLOUDWATCH_METRIC_BY_TYPE[metric_type],
                            "dimensionName": "ClusterName;Namespace;PodName",
                            "dimensionValue": f"{self.cluster_id};{namespace};{target_name}",
                            "targetMetricValue": str(target_value),
                            "minMetricValue": "0",
                            "metricStat": "Average",
                            "awsRegion": self.region,
                            "identityOwner": "operator",
                        },
                    }
                )

        if not triggers:
            triggers.append(
                {
                    "type": "cpu",
                    "metricType": "Utilization",
                    "metadata": {"value": "70"},
                }
            )

        return triggers

    def _apply_scaled_object(
        self,
        name: str,
        namespace: str,
        target_name: str,
        min_replicas: int,
        max_replicas: int,
        metrics_config: list[dict[str, Any]],
    ) -> None:
        """Create or patch a KEDA ScaledObject targeting one Deployment.

        Used whenever the metric set includes a GPU signal (see
        :meth:`_metrics_require_keda`). KEDA owns the backing HPA and reads GPU
        utilization from CloudWatch via the keda-operator's IRSA role, scaling
        ``target_name`` between ``min_replicas`` and ``max_replicas``. An
        already-present ScaledObject of the same name is merge-patched rather
        than duplicated.
        """
        body = {
            "apiVersion": f"{KEDA_API_GROUP}/{KEDA_API_VERSION}",
            "kind": "ScaledObject",
            "metadata": {
                "name": name,
                "namespace": namespace,
                "labels": {
                    "app": name,
                    "project": "gco",
                    "gco.io/type": "inference",
                },
            },
            "spec": {
                "scaleTargetRef": {"name": target_name},
                "minReplicaCount": min_replicas,
                "maxReplicaCount": max_replicas,
                "triggers": self._build_keda_triggers(metrics_config, target_name, namespace),
            },
        }

        custom = client.CustomObjectsApi()
        try:
            custom.create_namespaced_custom_object(
                group=KEDA_API_GROUP,
                version=KEDA_API_VERSION,
                namespace=namespace,
                plural=KEDA_SCALEDOBJECT_PLURAL,
                body=body,
                _request_timeout=self._k8s_timeout,
            )
            logger.info(
                "Created KEDA ScaledObject %s targeting %s (min=%d, max=%d)",
                name,
                target_name,
                min_replicas,
                max_replicas,
            )
        except ApiException as e:
            if e.status == 409:
                custom.patch_namespaced_custom_object(
                    group=KEDA_API_GROUP,
                    version=KEDA_API_VERSION,
                    namespace=namespace,
                    plural=KEDA_SCALEDOBJECT_PLURAL,
                    name=name,
                    body=body,
                    _request_timeout=self._k8s_timeout,
                )
                logger.info("Updated KEDA ScaledObject %s", name)
            else:
                raise

    def _apply_hpa(
        self,
        hpa_name: str,
        namespace: str,
        target_name: str,
        min_replicas: int,
        max_replicas: int,
        metrics_config: list[dict[str, Any]],
    ) -> None:
        """Create or patch a single autoscaler targeting one Deployment.

        Builds a V2 autoscaler that scales ``target_name`` between
        ``min_replicas`` and ``max_replicas`` on the given metrics, then creates
        it. An already-present autoscaler of the same name is patched in place
        rather than duplicated. When the metric set includes a GPU signal the
        autoscaler is materialized as a KEDA ScaledObject instead (native HPA
        Resource metrics cannot read GPU utilization).
        """
        if self._metrics_require_keda(metrics_config):
            self._apply_scaled_object(
                name=hpa_name,
                namespace=namespace,
                target_name=target_name,
                min_replicas=min_replicas,
                max_replicas=max_replicas,
                metrics_config=metrics_config,
            )
            return

        hpa = client.V2HorizontalPodAutoscaler(
            metadata=client.V1ObjectMeta(
                name=hpa_name,
                namespace=namespace,
                labels={
                    "app": hpa_name,
                    "project": "gco",
                    "gco.io/type": "inference",
                },
            ),
            spec=client.V2HorizontalPodAutoscalerSpec(
                scale_target_ref=client.V2CrossVersionObjectReference(
                    api_version="apps/v1",
                    kind="Deployment",
                    name=target_name,
                ),
                min_replicas=min_replicas,
                max_replicas=max_replicas,
                metrics=self._build_hpa_metrics(metrics_config),
            ),
        )

        autoscaling_v2 = client.AutoscalingV2Api()
        try:
            autoscaling_v2.create_namespaced_horizontal_pod_autoscaler(namespace, hpa)
            logger.info(
                "Created HPA %s targeting %s (min=%d, max=%d)",
                hpa_name,
                target_name,
                min_replicas,
                max_replicas,
            )
        except ApiException as e:
            if e.status == 409:
                autoscaling_v2.patch_namespaced_horizontal_pod_autoscaler(hpa_name, namespace, hpa)
                logger.info("Updated HPA %s", hpa_name)
            else:
                raise

    def _create_or_update_hpa(self, name: str, namespace: str, spec: dict[str, Any]) -> None:
        """Create or update a Horizontal Pod Autoscaler for an inference endpoint."""
        autoscaling_config = spec.get("autoscaling", {})
        if not autoscaling_config.get("enabled"):
            return

        min_replicas = autoscaling_config.get("min_replicas", 1)
        max_replicas = autoscaling_config.get("max_replicas", 10)
        metrics_config = autoscaling_config.get("metrics", [{"type": "cpu", "target": 70}])

        self._apply_hpa(
            hpa_name=name,
            namespace=namespace,
            target_name=name,
            min_replicas=min_replicas,
            max_replicas=max_replicas,
            metrics_config=metrics_config,
        )

    def _create_role_hpa(self, name: str, ns: str, spec: dict[str, Any], role: str) -> None:
        """Create or update one autoscaler for a single Mooncake role.

        When the endpoint's ``mooncake.autoscaling`` block is enabled and
        carries a config for this role, this materializes exactly one autoscaler
        named ``{name}-{role}`` that scales the matching ``{name}-{role}``
        Deployment between the role's ``min_replicas`` and ``max_replicas``. The
        role Deployment itself is already materialized at ``min_replicas`` by
        :meth:`_replica_count_for_role`, so the autoscaler owns the count from
        that lower bound. When autoscaling is absent or disabled, or when the
        role carries no config, no autoscaler is created and the role's replicas
        stay at their topology value.

        Args:
            name: The endpoint name.
            ns: The namespace the role Deployment lives in.
            spec: The endpoint spec; ``spec["mooncake"]["autoscaling"]`` drives
                the bounds and metrics.
            role: One of ``"prefill"`` or ``"decode"``.
        """
        mooncake = spec.get("mooncake") or {}
        autoscaling = mooncake.get("autoscaling") or {}
        if not autoscaling.get("enabled"):
            return

        role_cfg = autoscaling.get(role)
        if not role_cfg:
            return

        min_replicas = role_cfg.get("min_replicas", 1)
        max_replicas = role_cfg.get("max_replicas", 10)
        metrics_config = role_cfg.get("metrics", [{"type": "cpu", "target": 70}])

        target_name = f"{name}-{role}"
        self._apply_hpa(
            hpa_name=target_name,
            namespace=ns,
            target_name=target_name,
            min_replicas=min_replicas,
            max_replicas=max_replicas,
            metrics_config=metrics_config,
        )

    # ------------------------------------------------------------------
    # Metrics
    # ------------------------------------------------------------------

    def get_metrics(self) -> dict[str, Any]:
        return {
            "cluster_id": self.cluster_id,
            "region": self.region,
            "running": self._running,
            "reconcile_count": self._reconcile_count,
            "errors_count": self._errors_count,
        }


def create_inference_monitor_from_env() -> InferenceMonitor:
    """Create an InferenceMonitor from environment variables."""
    cluster_id = os.getenv("CLUSTER_NAME", "unknown-cluster")
    region = os.getenv("REGION", "unknown-region")
    namespace = os.getenv("INFERENCE_NAMESPACE", "gco-inference")
    interval = int(os.getenv("RECONCILE_INTERVAL_SECONDS", "15"))

    # Enable structured JSON logging for CloudWatch Insights
    configure_structured_logging(
        service_name="inference-monitor",
        cluster_id=cluster_id,
        region=region,
    )

    store = InferenceEndpointStore()  # Uses DYNAMODB_REGION env var, falls back to REGION

    return InferenceMonitor(
        cluster_id=cluster_id,
        region=region,
        store=store,
        namespace=namespace,
        reconcile_interval=interval,
    )


async def main() -> None:
    """Entry point for the inference monitor."""
    monitor = create_inference_monitor_from_env()
    logger.info("Inference monitor initialized: %s", monitor.get_metrics())

    while True:
        try:
            await monitor.start()
        except KeyboardInterrupt:
            logger.info("Shutting down inference monitor")
            monitor.stop()
            break
        except Exception as e:
            logger.error("Monitor crashed, restarting in 10s: %s", e, exc_info=True)
            monitor.stop()
            monitor._running = False
            await asyncio.sleep(10)


if __name__ == "__main__":
    asyncio.run(main())
