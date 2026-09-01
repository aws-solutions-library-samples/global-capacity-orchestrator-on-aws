"""
Inference Monitor — reconciliation controller for inference endpoints.

Runs in each regional EKS cluster and polls the global DynamoDB table
(gco-inference-endpoints) to reconcile desired state with actual
Kubernetes resources. Follows a GitOps-style reconciliation pattern:

    DynamoDB (desired state) → inference_monitor → Kubernetes (actual state)

The monitor:
- Creates and reconciles Deployments, ClusterIP Services, and optional autoscalers
- Leaves public routing on the shared ``gco-system/gco-gateway`` HTTPRoute:
  ``/inference`` -> ``gco-system/inference-proxy``
- Removes legacy endpoint-specific Ingresses so upgrades cannot retain a bypass
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
import contextlib
import json
import logging
import os
import re
import secrets
import signal
import threading
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from functools import partial
from pathlib import Path
from typing import Any, NoReturn, TypedDict
from urllib.parse import urlsplit

from kubernetes import client, config
from kubernetes.client.models import V1Deployment
from kubernetes.client.rest import ApiException

from gco.services.inference_store import InferenceEndpointStore
from gco.services.structured_logging import configure_structured_logging

# <pyflowchart-code-diagram> BEGIN - auto-inserted, do not edit
# Generated at (UTC): 2026-09-01T13:22:56Z
# Generated from Git commit: ed395032d46063f44b638deb85ae2a6dbf98e7f4
# Flowchart(s) generated from this file:
#   * ``InferenceMonitor._reconcile_endpoint_authorized`` -> ``diagrams/code_diagrams/gco/services/inference_monitor.InferenceMonitor__reconcile_endpoint_authorized.html``
#     (PNG: ``diagrams/code_diagrams/gco/services/inference_monitor.InferenceMonitor__reconcile_endpoint_authorized.png``)
# Regenerate with ``SOURCE_DATE_EPOCH=<unix-seconds> GCO_DIAGRAM_SOURCE_COMMIT=<40-char-sha> python diagrams/generate.py --code-only``.
# <pyflowchart-code-diagram> END


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
    """A user-named proxy admin API key Secret is missing or empty.

    Raised before the prefill-decode proxy is materialized when a Secret named
    by ``proxy.admin_api_key_secret`` is absent or carries no usable
    ``ADMIN_API_KEY`` value. An endpoint that names no Secret takes the separate
    auto-managed path and receives a generated ``{name}-admin`` Secret. The
    proxy never starts without a usable key, so no proxy Deployment or Service
    is created on this error. ``secret`` records the rejected Secret name.
    """

    def __init__(self, secret: str | None, reason: str):
        self.secret = secret
        self.reason = reason
        named = repr(secret) if secret else "<unnamed>"
        super().__init__(f"Admin API key Secret {named} is unusable: {reason}")


# Official AWS CLI v2 multi-architecture image. Keep the readable release tag
# and immutable manifest-list digest together: both amd64 and arm64 inference
# nodes resolve through this single verified index.
AWS_CLI_IMAGE = (
    "public.ecr.aws/aws-cli/aws-cli:2.36.26@"
    "sha256:eaa5d4d024c9b83fe4af2aae3068b052f096beed1a41f202d480c5c521aa3378"
)

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

# Mooncake KV-transfer role pods are pinned to a dedicated EFA NodePool
# (mooncake-efa-pool, manifest 46-nodepool-mooncake-efa.yaml) that only offers
# instance families with >=80GB of GPU memory and FP8-capable Hopper/Blackwell
# GPUs. The shared training EFA pool (43-nodepool-efa.yaml) also offers p4d
# (A100 40GB, Ampere, no FP8), which is too small for many disaggregated/store
# models and can be selected by Karpenter whenever a pod asks only for efa=true.
# Selecting this extra label keeps role pods off p4d without disturbing the
# training pool. The value must match the label on the dedicated NodePool.
MOONCAKE_EFA_NODE_SELECTOR_KEY = "mooncake-efa"
MOONCAKE_EFA_NODE_SELECTOR_VALUE = "true"

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
NETWORK_POLICY_INFERENCE_INTERNAL = "allow-inference-internal"
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

# SSM namespace suffix publishing the always-on general-purpose regional
# bucket's discovery values for a region. The full namespace is
# ``/<project_name>/regional-shared-bucket`` (see
# ``constants.regional_shared_ssm_parameter_prefix``); the monitor builds it at
# runtime from the injected ``PROJECT_NAME`` env var rather than importing the
# CDK constant, so it needs no infrastructure imports at runtime. Kept as a
# suffix constant so the ``/name``, ``/arn``, ``/region`` contract stays in one
# place. See ``_regional_shared_ssm_parameter_prefix``.
REGIONAL_SHARED_SSM_PARAMETER_SUFFIX = "regional-shared-bucket"


def _regional_shared_ssm_parameter_prefix() -> str:
    """Return this deployment's regional-shared-bucket SSM namespace.

    Built from the ``PROJECT_NAME`` environment variable (default ``"gco"``)
    so the monitor reads the same project-scoped path the regional stack
    writes (``/<project_name>/regional-shared-bucket``). Mirrors
    ``constants.regional_shared_ssm_parameter_prefix`` without importing CDK.
    """
    project_name = os.environ.get("PROJECT_NAME", "gco")
    return f"/{project_name}/{REGIONAL_SHARED_SSM_PARAMETER_SUFFIX}"


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

# TCP port the proxy container listens on for authenticated serving requests.
PD_PROXY_PORT = 8000

# Environment variable the proxy reads its admin key from, and the data key the
# backing Kubernetes Secret stores it under. The key value is delivered to the
# container through a Secret reference at pod start — it is never written to the
# endpoint spec or passed as a command-line argument.
PD_PROXY_ADMIN_API_KEY_ENV = "ADMIN_API_KEY"
ADMIN_API_KEY_SECRET_DATA_KEY = "ADMIN_API_KEY"

# The proxy program (gco/services/mooncake_pd_proxy.py) is shipped to the proxy
# pod as a ConfigMap and run from this mount path. The prefill/decode backend
# URLs and the listen port are passed to it through these env vars; it routes to
# the role pods through their in-cluster Services so kube-proxy load-balances
# across only the Ready endpoints of each role.
PD_PROXY_SCRIPT_FILENAME = "mooncake_pd_proxy.py"
PD_PROXY_CONFIG_MOUNT_DIR = "/etc/pd-proxy"
PD_PROXY_SCRIPT_PATH = f"{PD_PROXY_CONFIG_MOUNT_DIR}/{PD_PROXY_SCRIPT_FILENAME}"
PD_PROXY_PORT_ENV = "PD_PROXY_PORT"
PD_PROXY_PREFILL_URL_ENV = "PD_PROXY_PREFILL_URL"
PD_PROXY_DECODE_URL_ENV = "PD_PROXY_DECODE_URL"


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


class RegionStatusConditions(TypedDict, total=False):
    """Optional DynamoDB predicates shared by every regional status write."""

    expected_lifecycle_id: str
    expected_region_generation: str
    expected_deletion_generation: str


_LIFECYCLE_ANNOTATION = "gco.io/lifecycle-id"
_REGION_GENERATION_ANNOTATION = "gco.io/region-generation"
_LEADER_EPOCH_ANNOTATION = "gco.io/leader-epoch"


class ReconcileFencedError(RuntimeError):
    """The caller no longer owns the endpoint or leader epoch it read."""


@dataclass(frozen=True)
class ReconcileAuthority:
    """Immutable authority carried by one endpoint reconciliation pass."""

    endpoint_name: str
    lifecycle_id: str
    region_generation: str
    leader_epoch: str
    deletion_generation: str | None = None
    deleting: bool = False
    region_removed: bool = False

    @property
    def annotations(self) -> dict[str, str]:
        return {
            _LIFECYCLE_ANNOTATION: self.lifecycle_id,
            _REGION_GENERATION_ANNOTATION: self.region_generation,
            _LEADER_EPOCH_ANNOTATION: self.leader_epoch,
        }


@dataclass(frozen=True)
class EndpointResourceInventory:
    """Deterministic names of every top-level Kubernetes object owned by an endpoint."""

    deployments: tuple[str, ...]
    services: tuple[str, ...]
    horizontal_pod_autoscalers: tuple[str, ...]
    scaled_objects: tuple[str, ...]
    config_maps: tuple[str, ...]
    legacy_ingresses: tuple[str, ...]
    legacy_http_routes: tuple[str, ...]
    generated_admin_secret: str


@dataclass(frozen=True)
class ResourceCleanupResult:
    """Observed result of one idempotent endpoint cleanup pass.

    A successful Kubernetes delete only starts asynchronous deletion. ``pending``
    therefore contains objects still returned by a read-after-delete, while
    ``errors`` contains sanitized request failures. Cleanup is complete only
    when every owned object is independently observed absent and no request
    failed during the pass.
    """

    pending: tuple[str, ...] = ()
    errors: tuple[str, ...] = ()
    resources_found: bool = False

    @property
    def complete(self) -> bool:
        return not self.pending and not self.errors

    @property
    def error_message(self) -> str | None:
        if not self.errors:
            return None
        return "Endpoint cleanup could not be completed: " + "; ".join(self.errors)


def _resolved_mooncake_transfer(mooncake: dict[str, Any]) -> tuple[str, str]:
    """Resolve the persisted transfer intent and optional network device.

    GCO's spec deliberately uses ``rdma`` as the portable high-performance
    intent because the mounted Mooncake store configuration accepts
    ``rdma|tcp``. On AWS, role pods with that intent are placed on EFA nodes,
    so :func:`build_kv_transfer_config` translates it to vLLM's explicit
    ``mooncake_protocol=efa`` at the point-to-point connector boundary.
    """
    transfer = mooncake.get("transfer", {})
    if not isinstance(transfer, dict):
        raise ValueError("mooncake.transfer must be a mapping")

    protocol = transfer.get("protocol", "rdma")
    if protocol not in {"rdma", "tcp"}:
        raise ValueError(
            f"mooncake.transfer.protocol must be one of {{rdma, tcp}}, got {protocol!r}"
        )
    device_name = transfer.get("device_name", "")
    if not isinstance(device_name, str):
        raise ValueError(f"mooncake.transfer.device_name must be a string, got {device_name!r}")
    return protocol, device_name


def build_kv_transfer_config(mooncake: dict[str, Any], role: str) -> str:
    """Return the JSON string for vLLM's ``--kv-transfer-config``.

    Translates a mooncake spec block plus a worker role into the connector
    configuration vLLM expects:

    - ``disaggregated`` emits a ``MooncakeConnector``.
    - ``store`` emits a ``MooncakeStoreConnector``.
    - ``both`` emits a ``MultiConnector`` wrapping a ``MooncakeConnector``
      (index 0) followed by a ``MooncakeStoreConnector`` (index 1), both
      sharing the role's ``kv_role``.

    Every point-to-point ``MooncakeConnector`` receives explicit
    ``kv_connector_extra_config``. GCO's default/high-performance ``rdma``
    intent maps to Mooncake's AWS-specific ``efa`` protocol because the same
    pod is pinned to the EFA node pool; ``tcp`` remains an explicit fallback.
    ``device_name`` is forwarded verbatim, with an empty string requesting
    Mooncake/libfabric auto-detection.

    The emitted ``kv_role`` is ``kv_producer`` for prefill, ``kv_consumer``
    for decode, and ``kv_both`` for a single store instance.

    Args:
        mooncake: The ``spec["mooncake"]`` block; its ``mode`` selects the
            connector shape and its optional ``transfer`` block selects the
            protocol/device.
        role: One of ``"prefill"``, ``"decode"``, or ``"single"``.

    Returns:
        A JSON object string parseable by vLLM.

    Raises:
        ValueError: If the ``(mode, role)`` combination or transfer settings
            are unsupported. No configuration is emitted in that case.
    """
    mode = mooncake.get("mode")
    supported_roles = _WORKER_ROLES_BY_MODE.get(mode) if isinstance(mode, str) else None
    if supported_roles is None or role not in supported_roles:
        raise ValueError(f"Unsupported (mode, role) pair: ({mode!r}, {role!r})")

    kv_role = _KV_ROLE_BY_WORKER_ROLE[role]

    if mode == "store":
        return json.dumps({"kv_connector": "MooncakeStoreConnector", "kv_role": kv_role})

    protocol, device_name = _resolved_mooncake_transfer(mooncake)
    connector = {
        "kv_connector": "MooncakeConnector",
        "kv_role": kv_role,
        "kv_connector_extra_config": {
            "mooncake_protocol": "efa" if protocol == "rdma" else "tcp",
            "device_name": device_name,
        },
    }
    if mode == "disaggregated":
        return json.dumps(connector)

    # mode == "both": MultiConnector chains transfer then store.
    return json.dumps(
        {
            "kv_connector": "MultiConnector",
            "kv_role": kv_role,
            "kv_connector_extra_config": {
                "connectors": [
                    connector,
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
    protocol, device_name = _resolved_mooncake_transfer(mooncake)
    store = mooncake.get("store", {})
    cfg: dict[str, Any] = {
        "metadata_server": region_services["metadata_server"],
        "protocol": protocol,
        "device_name": device_name,
    }
    if store.get("enabled"):
        cfg["master_server_address"] = region_services["master_server_address"]
        # The store runs embedded in each vLLM pod (every rank contributes
        # `global_segment_size` to the shared pool; GCO's per-region
        # mooncake-master is only the metadata/master coordinator, not a
        # standalone store that owns the pool). Embedded mode rejects a zero
        # segment, so default to 4 GiB (the upstream default) when the spec
        # does not set one; an operator can tune it via configure-store.
        cfg["global_segment_size"] = store.get("global_segment_size", "4294967296")
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
    - add an ``efa=true`` node selector plus a ``mooncake-efa=true`` node
      selector (merged with any existing selectors), and
    - request at least one ``vpc.amazonaws.com/efa`` device on the pod's
      containers, leaving every existing resource request and limit — including
      GPU asks — untouched.

    The ``mooncake-efa=true`` selector pins the pod to the dedicated
    ``mooncake-efa-pool`` NodePool, which only offers instance families with
    >=80GB of GPU memory and FP8-capable Hopper/Blackwell GPUs. This keeps role
    pods off the A100-40GB ``p4d`` family that the shared training EFA pool
    still offers — that family OOMs on many models and cannot run FP8 KV-cache
    configs, so Karpenter selecting it for a mooncake pod is a latent failure.

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
    protocol, _device_name = _resolved_mooncake_transfer(mooncake)
    if protocol != "rdma":
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

    # Merge the EFA node selectors with any selectors already in place. The
    # generic efa=true selector lands the pod on EFA fabric; mooncake-efa=true
    # narrows that to the dedicated mooncake-efa-pool, which excludes the
    # A100-40GB p4d family that the shared training EFA pool still offers.
    node_selector = dict(pod_spec.node_selector or {})
    node_selector[EFA_NODE_SELECTOR_KEY] = EFA_NODE_SELECTOR_VALUE
    node_selector[MOONCAKE_EFA_NODE_SELECTOR_KEY] = MOONCAKE_EFA_NODE_SELECTOR_VALUE
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
        self.discovery_v1 = client.DiscoveryV1Api()

        # Timeout for Kubernetes API calls (seconds)
        self._k8s_timeout = int(os.environ.get("K8S_API_TIMEOUT", "30"))

        # Health watchdog: tracks when each endpoint first became unready.
        # Inference traffic enters through the shared ``gco-system/gco-gateway``
        # HTTPRoute at ``/inference`` and then ``gco-system/inference-proxy``, so
        # model readiness never mutates shared Gateway API resources. Once this
        # threshold is exceeded, reconciliation emits an explicit degraded-state
        # warning while the proxy continues returning 503 until a replica is ready.
        self._unready_since: dict[str, datetime] = {}
        self._unhealthy_threshold_seconds = int(
            os.environ.get("INFERENCE_UNHEALTHY_THRESHOLD_SECONDS", "300")
        )  # 5 minutes default

        # Master-readiness gate: tracks when each store-bearing endpoint first
        # deferred its role-pod creation because the shared master was not yet
        # Ready. The entry is cleared once the master reports a Ready replica so
        # a later restart of the master restarts the clock cleanly.
        self._master_deferral_since: dict[str, datetime] = {}

        # Leader authority is renewed from a dedicated thread while reconcile
        # performs synchronous Kubernetes calls. The per-acquisition epoch is
        # also stamped on endpoint-owned objects; object UID/resourceVersion
        # preconditions remain the hard fence if a process resumes after losing
        # the Lease.
        self._lease_name: str | None = None
        self._lease_holder: str | None = None
        self._leader_epoch: str | None = None
        self._leadership_lost = threading.Event()
        self._active_authority: ReconcileAuthority | None = None

        # Metrics
        self._reconcile_count = 0
        self._errors_count = 0

    # ------------------------------------------------------------------
    # Reconciliation loop
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """Start reconciliation under an independently renewed leader Lease."""
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

        pod_name = os.environ.get("HOSTNAME", f"monitor-{id(self)}")
        lease_name = "inference-monitor-leader"
        self._lease_name = lease_name
        self._lease_holder = pod_name

        while self._running:
            try:
                if self._try_acquire_lease(lease_name, pod_name):
                    with self._renewing_leadership():
                        await self.reconcile()
                else:
                    logger.debug("Not the leader, waiting...")
            except ReconcileFencedError as error:
                logger.warning("Reconciliation stopped after authority loss: %s", error)
            except Exception as error:
                logger.error("Reconciliation error: %s", error, exc_info=True)
                self._errors_count += 1
            try:
                await asyncio.sleep(self.reconcile_interval)
            except Exception as error:
                logger.error("Sleep interrupted: %s", error)
                break

    @staticmethod
    def _lease_annotations(lease: Any) -> dict[str, str]:
        metadata = getattr(lease, "metadata", None)
        annotations = getattr(metadata, "annotations", None)
        return dict(annotations) if isinstance(annotations, dict) else {}

    def _lease_is_expired(self, lease: Any, now: datetime) -> bool:
        renew_time = getattr(getattr(lease, "spec", None), "renew_time", None)
        if renew_time is None:
            return True
        if renew_time.tzinfo is None:
            renew_time = renew_time.replace(tzinfo=UTC)
        duration = getattr(lease.spec, "lease_duration_seconds", None)
        if not isinstance(duration, int) or isinstance(duration, bool) or duration <= 0:
            duration = self.reconcile_interval * 3
        return bool((now - renew_time).total_seconds() >= int(duration))

    def _stamp_lease_epoch(self, lease: Any, epoch: str) -> None:
        metadata = getattr(lease, "metadata", None)
        if metadata is None:
            lease.metadata = client.V1ObjectMeta()
            metadata = lease.metadata
        annotations = self._lease_annotations(lease)
        annotations[_LEADER_EPOCH_ANNOTATION] = epoch
        metadata.annotations = annotations

    def _try_acquire_lease(self, lease_name: str, holder: str) -> bool:
        """Acquire/renew a Lease and establish one stable acquisition epoch."""
        coordination_v1 = client.CoordinationV1Api()
        now = datetime.now(UTC)
        lease_duration = self.reconcile_interval * 3
        try:
            lease = coordination_v1.read_namespaced_lease(lease_name, self.namespace)
            current_holder = lease.spec.holder_identity
            expired = self._lease_is_expired(lease, now)
            annotations = self._lease_annotations(lease)
            observed_epoch = annotations.get(_LEADER_EPOCH_ANNOTATION)

            if current_holder == holder and not expired:
                # Adopt the persisted epoch after a harmless local restart of
                # the loop, but never renew a same-name holder with a different
                # in-memory epoch.
                if self._leader_epoch is None:
                    self._leader_epoch = observed_epoch or secrets.token_hex(32)
                elif observed_epoch and observed_epoch != self._leader_epoch:
                    self._leadership_lost.set()
                    return False
                epoch = self._leader_epoch
            elif current_holder in (None, "") or expired:
                epoch = secrets.token_hex(32)
                self._leader_epoch = epoch
                lease.spec.holder_identity = holder
                lease.spec.acquire_time = now
                transitions = getattr(lease.spec, "lease_transitions", None)
                lease.spec.lease_transitions = int(transitions or 0) + 1
                logger.info("Acquiring leader lease as %s with a new epoch", holder)
            else:
                return False

            lease.spec.renew_time = now
            lease.spec.lease_duration_seconds = lease_duration
            self._stamp_lease_epoch(lease, epoch)
            coordination_v1.replace_namespaced_lease(lease_name, self.namespace, lease)
            self._lease_name = lease_name
            self._lease_holder = holder
            self._leadership_lost.clear()
            return True
        except ApiException as error:
            if error.status == 404:
                epoch = secrets.token_hex(32)
                lease = client.V1Lease(
                    metadata=client.V1ObjectMeta(
                        name=lease_name,
                        namespace=self.namespace,
                        annotations={_LEADER_EPOCH_ANNOTATION: epoch},
                    ),
                    spec=client.V1LeaseSpec(
                        holder_identity=holder,
                        lease_duration_seconds=lease_duration,
                        acquire_time=now,
                        renew_time=now,
                        lease_transitions=0,
                    ),
                )
                try:
                    coordination_v1.create_namespaced_lease(self.namespace, lease)
                except ApiException:
                    return False
                self._leader_epoch = epoch
                self._lease_name = lease_name
                self._lease_holder = holder
                self._leadership_lost.clear()
                logger.info("Created leader lease as %s", holder)
                return True
            if error.status == 409:
                logger.info("Lost leader Lease optimistic-concurrency race")
                self._leadership_lost.set()
                return False
            logger.warning("Lease check failed: %s", error.reason)
            self._leadership_lost.set()
            return False

    def _renew_current_lease(self) -> bool:
        """Renew only the exact holder/epoch currently owned by this process."""
        lease_name = self._lease_name
        holder = self._lease_holder
        epoch = self._leader_epoch
        if not all(isinstance(value, str) and value for value in (lease_name, holder, epoch)):
            return False
        coordination_v1 = client.CoordinationV1Api()
        now = datetime.now(UTC)
        try:
            lease = coordination_v1.read_namespaced_lease(lease_name, self.namespace)
            if (
                lease.spec.holder_identity != holder
                or self._lease_annotations(lease).get(_LEADER_EPOCH_ANNOTATION) != epoch
                or self._lease_is_expired(lease, now)
            ):
                return False
            lease.spec.renew_time = now
            coordination_v1.replace_namespaced_lease(lease_name, self.namespace, lease)
            return True
        except Exception:
            logger.warning("Leader Lease renewal failed", exc_info=True)
            return False

    def _lease_renewal_loop(self, stop_event: threading.Event) -> None:
        interval = max(1.0, float(self.reconcile_interval))
        while not stop_event.wait(interval):
            if not self._renew_current_lease():
                self._leadership_lost.set()
                return

    @contextlib.contextmanager
    def _renewing_leadership(self) -> Iterator[None]:
        """Renew the Lease outside the asyncio loop while one pass is running."""
        stop_event = threading.Event()
        renewal = threading.Thread(
            target=self._lease_renewal_loop,
            args=(stop_event,),
            name="inference-monitor-lease-renewer",
            daemon=True,
        )
        renewal.start()
        try:
            yield
        finally:
            stop_event.set()
            renewal.join(timeout=max(1.0, float(self.reconcile_interval)))

    def _assert_current_leadership(self) -> None:
        """Fail closed before mutation when the persisted Lease epoch changed."""
        if self._leadership_lost.is_set():
            raise ReconcileFencedError("leader Lease was lost")
        # Direct method-level unit tests do not enter start(); production does.
        if self._lease_name is None or self._lease_holder is None or self._leader_epoch is None:
            return
        coordination_v1 = client.CoordinationV1Api()
        try:
            lease = coordination_v1.read_namespaced_lease(self._lease_name, self.namespace)
        except Exception as error:
            self._leadership_lost.set()
            raise ReconcileFencedError("leader Lease could not be verified") from error
        if (
            lease.spec.holder_identity != self._lease_holder
            or self._lease_annotations(lease).get(_LEADER_EPOCH_ANNOTATION) != self._leader_epoch
            or self._lease_is_expired(lease, datetime.now(UTC))
        ):
            self._leadership_lost.set()
            raise ReconcileFencedError("leader Lease holder or epoch changed")

    def stop(self) -> None:
        """Stop the reconciliation loop and prevent additional mutations."""
        self._running = False
        self._leadership_lost.set()
        logger.info("Inference monitor stopped")

    @staticmethod
    def _lifecycle_metadata_complete(endpoint: dict[str, Any]) -> bool:
        """Return whether an endpoint has complete immutable cleanup metadata."""
        lifecycle_id = endpoint.get("lifecycle_id")
        cleanup_regions = endpoint.get("cleanup_regions")
        region_generations = endpoint.get("region_generations")
        return (
            isinstance(lifecycle_id, str)
            and bool(lifecycle_id)
            and isinstance(cleanup_regions, list)
            and isinstance(region_generations, dict)
            and all(
                isinstance(region, str)
                and bool(region)
                and isinstance(region_generations.get(region), str)
                and bool(region_generations[region])
                for region in cleanup_regions
            )
        )

    def _status_write_conditions(
        self,
        endpoint: dict[str, Any],
        *,
        deleting: bool = False,
    ) -> RegionStatusConditions:
        """Return lifecycle/generation predicates for one regional status write."""
        lifecycle_id = endpoint.get("lifecycle_id")
        if not isinstance(lifecycle_id, str) or not lifecycle_id:
            return {}
        conditions: RegionStatusConditions = {"expected_lifecycle_id": lifecycle_id}
        if deleting:
            generation = endpoint.get("deletion_generation")
            if isinstance(generation, str) and generation:
                conditions["expected_deletion_generation"] = generation
            return conditions
        raw_generations = endpoint.get("region_generations")
        generations = raw_generations if isinstance(raw_generations, dict) else {}
        region_generation = generations.get(self.region)
        if isinstance(region_generation, str) and region_generation:
            conditions["expected_region_generation"] = region_generation
        return conditions

    def _authority_from_endpoint(self, endpoint: dict[str, Any]) -> ReconcileAuthority | None:
        """Build object-level provenance for a production endpoint snapshot."""
        lifecycle_id = endpoint.get("lifecycle_id")
        generations = endpoint.get("region_generations")
        region_generation = generations.get(self.region) if isinstance(generations, dict) else None
        if not isinstance(lifecycle_id, str) or not lifecycle_id:
            if isinstance(endpoint.get("updated_at"), str):
                raise ReconcileFencedError("endpoint snapshot has no lifecycle authority")
            return None  # Compatibility for isolated method-level fixtures only.
        if not isinstance(region_generation, str) or not region_generation:
            if isinstance(endpoint.get("updated_at"), str):
                raise ReconcileFencedError("endpoint snapshot has no Region generation")
            return None
        desired_state = endpoint.get("desired_state")
        targets = endpoint.get("target_regions")
        target_regions = targets if isinstance(targets, list) else []
        deletion_generation = endpoint.get("deletion_generation")
        return ReconcileAuthority(
            endpoint_name=str(endpoint.get("endpoint_name", "")),
            lifecycle_id=lifecycle_id,
            region_generation=region_generation,
            leader_epoch=self._leader_epoch or f"direct-{lifecycle_id[:16]}",
            deletion_generation=(
                deletion_generation
                if isinstance(deletion_generation, str) and deletion_generation
                else None
            ),
            deleting=desired_state == "deleted",
            region_removed=desired_state != "deleted" and self.region not in target_regions,
        )

    def _strong_authority_matches(self, authority: ReconcileAuthority) -> bool:
        """Re-read DynamoDB before claiming legacy/previous-epoch objects."""
        try:
            latest = self.store.get_endpoint(authority.endpoint_name, consistent_read=True)
        except AttributeError, TypeError:
            return getattr(self, "_lease_name", None) is None
        if not isinstance(latest, dict):
            return getattr(self, "_lease_name", None) is None
        if latest.get("lifecycle_id") != authority.lifecycle_id:
            return False
        if authority.deleting:
            return (
                latest.get("desired_state") == "deleted"
                and latest.get("deletion_generation") == authority.deletion_generation
            )
        generations = latest.get("region_generations")
        return (
            latest.get("desired_state") != "deleted"
            and isinstance(generations, dict)
            and generations.get(self.region) == authority.region_generation
        )

    @staticmethod
    def _object_metadata(resource: Any) -> tuple[Any, dict[str, str], str | None, str | None]:
        """Return metadata, annotations, UID, and resourceVersion for typed/dict objects."""
        if isinstance(resource, dict):
            metadata = resource.get("metadata")
            metadata = metadata if isinstance(metadata, dict) else {}
            annotations = metadata.get("annotations")
            return (
                metadata,
                dict(annotations) if isinstance(annotations, dict) else {},
                metadata.get("uid") if isinstance(metadata.get("uid"), str) else None,
                (
                    metadata.get("resourceVersion")
                    if isinstance(metadata.get("resourceVersion"), str)
                    else None
                ),
            )
        metadata = getattr(resource, "metadata", None)
        annotations = getattr(metadata, "annotations", None)
        uid = getattr(metadata, "uid", None)
        resource_version = getattr(metadata, "resource_version", None)
        return (
            metadata,
            dict(annotations) if isinstance(annotations, dict) else {},
            uid if isinstance(uid, str) else None,
            resource_version if isinstance(resource_version, str) else None,
        )

    @staticmethod
    def _object_labels(resource: Any) -> dict[str, str]:
        if isinstance(resource, dict):
            metadata = resource.get("metadata")
            labels = metadata.get("labels") if isinstance(metadata, dict) else None
        else:
            metadata = getattr(resource, "metadata", None)
            labels = getattr(metadata, "labels", None)
        return dict(labels) if isinstance(labels, dict) else {}

    @classmethod
    def _has_monitor_provenance(cls, resource: Any) -> bool:
        labels = cls._object_labels(resource)
        return labels.get("project") == "gco" and labels.get("gco.io/type") == "inference"

    def _handoff_stale_resource(
        self,
        resource: Any,
        *,
        kind: str,
        resource_name: str,
        delete_resource: Callable[..., Any] | None,
        reason: str,
    ) -> NoReturn:
        """UID-delete a proven stale monitor object only for current DDB authority."""
        authority = getattr(self, "_active_authority", None)
        if (
            authority is None
            or delete_resource is None
            or not self._has_monitor_provenance(resource)
            or not self._strong_authority_matches(authority)
        ):
            raise ReconcileFencedError(f"{kind} {resource_name} {reason}")
        self._assert_current_leadership()
        try:
            delete_resource(
                body=self._delete_options_for(
                    resource,
                    kind=kind,
                    resource_name=resource_name,
                )
            )
        except ApiException as error:
            if error.status not in {404, 409}:
                raise
        raise ReconcileFencedError(
            f"{kind} {resource_name} stale authority handoff deletion requested"
        )

    def _authorize_resource(
        self,
        resource: Any,
        *,
        kind: str,
        resource_name: str,
        patch_metadata: Callable[..., Any] | None = None,
        read_resource: Callable[[], Any] | None = None,
        delete_resource: Callable[..., Any] | None = None,
        allow_region_mismatch: bool = False,
    ) -> Any:
        """Verify lifecycle provenance and CAS-claim the current leader epoch."""
        authority = getattr(self, "_active_authority", None)
        if authority is None:
            return resource
        self._assert_current_leadership()
        metadata, annotations, _uid, resource_version = self._object_metadata(resource)
        if getattr(self, "_lease_name", None) is None and resource_version is None:
            # Historical method-level fixtures use metadata-less MagicMocks.
            # Production reconciliation always has a Lease and real metadata.
            return resource
        observed_lifecycle = annotations.get(_LIFECYCLE_ANNOTATION)
        if observed_lifecycle is None and not self._has_monitor_provenance(resource):
            raise ReconcileFencedError(f"{kind} {resource_name} has ambiguous legacy ownership")
        if observed_lifecycle not in (None, authority.lifecycle_id):
            self._handoff_stale_resource(
                resource,
                kind=kind,
                resource_name=resource_name,
                delete_resource=delete_resource,
                reason="belongs to another endpoint lifecycle",
            )
        observed_region = annotations.get(_REGION_GENERATION_ANNOTATION)
        if not allow_region_mismatch and observed_region not in (None, authority.region_generation):
            self._handoff_stale_resource(
                resource,
                kind=kind,
                resource_name=resource_name,
                delete_resource=delete_resource,
                reason="belongs to another Region generation",
            )
        expected = authority.annotations
        if all(annotations.get(key) == value for key, value in expected.items()):
            return resource
        if patch_metadata is None or read_resource is None:
            raise ReconcileFencedError(f"{kind} {resource_name} lacks current immutable provenance")
        if not isinstance(resource_version, str) or not resource_version:
            raise ReconcileFencedError(
                f"{kind} {resource_name} has no resourceVersion for authority claim"
            )
        if not self._strong_authority_matches(authority):
            raise ReconcileFencedError("endpoint authority changed before Kubernetes mutation")
        self._assert_current_leadership()
        merged = dict(annotations)
        merged.update(expected)
        try:
            patch_metadata(
                body={
                    "metadata": {
                        "resourceVersion": resource_version,
                        "annotations": merged,
                    }
                }
            )
        except Exception as error:
            raise ReconcileFencedError(
                f"{kind} {resource_name} changed during authority claim"
            ) from error
        claimed = read_resource()
        _metadata, claimed_annotations, _claimed_uid, _claimed_version = self._object_metadata(
            claimed
        )
        if not all(claimed_annotations.get(key) == value for key, value in expected.items()):
            raise ReconcileFencedError(
                f"{kind} {resource_name} authority claim could not be verified"
            )
        return claimed

    def _provenance_annotations(self) -> dict[str, str] | None:
        authority = getattr(self, "_active_authority", None)
        return dict(authority.annotations) if authority is not None else None

    def _assert_mutation_authority(self) -> None:
        authority = getattr(self, "_active_authority", None)
        if authority is None:
            return
        self._assert_current_leadership()
        if not self._strong_authority_matches(authority):
            raise ReconcileFencedError("endpoint authority changed before Kubernetes mutation")

    def _confirm_created_resource(
        self,
        *,
        kind: str,
        resource_name: str,
        read_resource: Callable[[], Any],
        delete_resource: Callable[..., Any],
    ) -> Any:
        """Compensate an exact just-created object if Lease/DDB authority changed."""
        authority = getattr(self, "_active_authority", None)
        if authority is None:
            return None
        try:
            created = read_resource()
        except ApiException as error:
            if getattr(self, "_lease_name", None) is None and error.status == 404:
                return None
            raise
        _metadata, annotations, _uid, resource_version = self._object_metadata(created)
        if getattr(self, "_lease_name", None) is None and resource_version is None:
            return created
        if not all(annotations.get(key) == value for key, value in authority.annotations.items()):
            raise ReconcileFencedError(f"{kind} {resource_name} post-create provenance changed")
        try:
            self._assert_mutation_authority()
        except ReconcileFencedError:
            # This process created the exact annotated UID. Remove only that UID;
            # a replacement racing into the name makes the precondition fail and
            # is never touched.
            try:
                delete_resource(
                    body=self._delete_options_for(
                        created,
                        kind=kind,
                        resource_name=resource_name,
                    )
                )
            except ApiException as error:
                if error.status not in {404, 409}:
                    logger.warning(
                        "Post-create compensation failed for %s/%s: status %s",
                        kind,
                        resource_name,
                        error.status,
                    )
            raise
        return created

    def _delete_options_for(self, resource: Any, *, kind: str, resource_name: str) -> Any:
        """Build UID/resourceVersion delete preconditions for one authorized object."""
        _metadata, _annotations, uid, resource_version = self._object_metadata(resource)
        if uid and resource_version:
            return client.V1DeleteOptions(
                propagation_policy="Foreground",
                preconditions=client.V1Preconditions(
                    uid=uid,
                    resource_version=resource_version,
                ),
            )
        if (
            getattr(self, "_active_authority", None) is None
            or getattr(self, "_lease_name", None) is None
        ):
            return client.V1DeleteOptions(
                propagation_policy="Foreground",
                preconditions=client.V1Preconditions(uid=uid) if uid else None,
            )
        raise ReconcileFencedError(
            f"{kind} {resource_name} lacks UID/resourceVersion delete authority"
        )

    async def reconcile(self) -> list[dict[str, Any]]:
        """Run one reconciliation cycle with generation-fenced deletion."""
        self._reconcile_count += 1
        actions: list[dict[str, Any]] = []
        try:
            endpoints = self.store.list_endpoints()
        except Exception as e:
            logger.error("Failed to list endpoints from DynamoDB: %s", e)
            return actions

        # Records written before lifecycle fencing are upgraded from their
        # DynamoDB snapshot before any Kubernetes mutation. The update is
        # conditional on ``updated_at``; a concurrent writer wins and this pass
        # simply retries from the next scan. Direct method-level test fixtures
        # without a persistence timestamp remain outside this production path.
        normalized_endpoints: list[dict[str, Any]] = []
        for endpoint in endpoints:
            if not self._lifecycle_metadata_complete(endpoint) and isinstance(
                endpoint.get("updated_at"), str
            ):
                upgraded = self.store.ensure_lifecycle_metadata(endpoint)
                if not isinstance(upgraded, dict):
                    continue
                endpoint = upgraded
                actions.append(
                    {
                        "action": "initialize_lifecycle",
                        "endpoint": endpoint.get("endpoint_name", "unknown"),
                    }
                )
            normalized_endpoints.append(endpoint)
        endpoints = normalized_endpoints

        for endpoint in endpoints:
            name = endpoint.get("endpoint_name", "unknown")
            try:
                action = await self._reconcile_endpoint(endpoint)
                if action:
                    actions.append(action)
            except ReconcileFencedError as error:
                # Authority loss is not endpoint health. Another leader may
                # already have written the terminal acknowledgement; a stale
                # error write must never regress that quorum to ``error``.
                logger.warning("Stopped stale reconciliation for %s: %s", name, error)
                continue
            except Exception as e:
                logger.error("Failed to reconcile endpoint %s: %s", name, e)
                self._errors_count += 1
                status_conditions = self._status_write_conditions(
                    endpoint,
                    deleting=endpoint.get("desired_state") == "deleted",
                )
                self.store.update_region_status(
                    name,
                    self.region,
                    "error",
                    error=str(e),
                    **status_conditions,
                )

        # Any monitor may purge, but only from a strong snapshot containing a
        # fresh terminal acknowledgement for every immutable deletion member.
        # Each acknowledgement itself represents two child-complete inventory
        # sweeps, and the delete is conditioned on the same lifecycle,
        # generation, and updated_at snapshot.
        for endpoint in endpoints:
            if endpoint.get("desired_state") != "deleted":
                continue
            ep_name = endpoint.get("endpoint_name")
            if not isinstance(ep_name, str):
                continue
            try:
                latest = self.store.get_endpoint(ep_name, consistent_read=True)
            except Exception as e:
                logger.warning("Failed to refresh deleted endpoint %s: %s", ep_name, e)
                continue
            if not isinstance(latest, dict) or latest.get("desired_state") != "deleted":
                continue
            lifecycle_id = latest.get("lifecycle_id")
            generation = latest.get("deletion_generation")
            deletion_regions = latest.get("deletion_regions")
            updated_at = latest.get("updated_at")
            if not all(
                isinstance(value, str) and value for value in (lifecycle_id, generation, updated_at)
            ):
                continue
            if not isinstance(deletion_regions, list) or not deletion_regions:
                continue
            cleanup_regions = {
                region for region in deletion_regions if isinstance(region, str) and region
            }
            if len(cleanup_regions) != len(deletion_regions):
                continue
            raw_status = latest.get("region_status")
            region_status = raw_status if isinstance(raw_status, dict) else {}
            if not all(
                isinstance(region_status.get(region), dict)
                and region_status[region].get("state") == "deleted"
                and region_status[region].get("lifecycle_id") == lifecycle_id
                and region_status[region].get("deletion_generation") == generation
                and region_status[region].get("absence_observations", 0) >= 2
                for region in cleanup_regions
            ):
                continue
            try:
                deleted = self.store.delete_endpoint(
                    ep_name,
                    expected_updated_at=updated_at,
                    expected_lifecycle_id=lifecycle_id,
                    expected_deletion_generation=generation,
                )
                if not deleted:
                    continue
                logger.info(
                    "Purged endpoint %s lifecycle %s generation %s",
                    ep_name,
                    lifecycle_id,
                    generation,
                )
                actions.append({"action": "purge", "endpoint": ep_name})
            except Exception as e:
                logger.warning("Failed to purge endpoint %s: %s", ep_name, e)
        return actions

    @staticmethod
    def _cleanup_ack_is_terminal(endpoint: dict[str, Any], region: str) -> bool:
        """Return whether this region already durably acknowledged this generation."""
        raw_statuses = endpoint.get("region_status")
        statuses = raw_statuses if isinstance(raw_statuses, dict) else {}
        status = statuses.get(region)
        if not isinstance(status, dict) or status.get("state") != "deleted":
            return False
        lifecycle_id = endpoint.get("lifecycle_id")
        if status.get("lifecycle_id") != lifecycle_id:
            return False
        if endpoint.get("desired_state") == "deleted":
            return (
                status.get("deletion_generation") == endpoint.get("deletion_generation")
                and status.get("absence_observations", 0) >= 2
            )
        raw_generations = endpoint.get("region_generations")
        generations = raw_generations if isinstance(raw_generations, dict) else {}
        return (
            status.get("region_generation") == generations.get(region)
            and status.get("absence_observations", 0) >= 2
        )

    def _record_cleanup_observation(
        self,
        endpoint: dict[str, Any],
        cleanup: ResourceCleanupResult,
    ) -> tuple[str, bool]:
        """Persist one stable-absence observation for the active lifecycle/generation."""
        lifecycle_id = endpoint.get("lifecycle_id")
        if not isinstance(lifecycle_id, str) or not lifecycle_id:
            raise RuntimeError("Endpoint cleanup requires an immutable lifecycle id")
        deleting = endpoint.get("desired_state") == "deleted"
        generation = endpoint.get("deletion_generation") if deleting else None
        raw_region_generations = endpoint.get("region_generations")
        region_generations = (
            raw_region_generations if isinstance(raw_region_generations, dict) else {}
        )
        region_generation = region_generations.get(self.region) if not deleting else None
        if deleting and (not isinstance(generation, str) or not generation):
            raise RuntimeError("Endpoint deletion requires an immutable deletion generation")
        if not deleting and (not isinstance(region_generation, str) or not region_generation):
            raise RuntimeError("Endpoint cleanup requires a current Region generation")

        raw_statuses = endpoint.get("region_status")
        statuses = raw_statuses if isinstance(raw_statuses, dict) else {}
        previous = statuses.get(self.region)
        same_generation = (
            isinstance(previous, dict)
            and previous.get("lifecycle_id") == lifecycle_id
            and (
                previous.get("deletion_generation") == generation
                if deleting
                else previous.get("region_generation") == region_generation
            )
        )
        previous_observations = (
            int(previous.get("absence_observations", 0))
            if same_generation and isinstance(previous, dict)
            else 0
        )
        observations = previous_observations + 1 if cleanup.complete else 0
        state = "deleted" if observations >= 2 else "deleting"
        status_kwargs: dict[str, Any] = {
            "extra": {"absence_observations": observations},
            "expected_lifecycle_id": lifecycle_id,
        }
        if deleting:
            status_kwargs["expected_deletion_generation"] = generation
        else:
            status_kwargs["expected_region_generation"] = region_generation
        if cleanup.error_message:
            status_kwargs["error"] = cleanup.error_message
        written = self.store.update_region_status(
            endpoint["endpoint_name"],
            self.region,
            state,
            **status_kwargs,
        )
        return state, bool(written)

    async def _reconcile_endpoint(self, endpoint: dict[str, Any]) -> dict[str, Any] | None:
        """Reconcile one endpoint under immutable Kubernetes provenance."""
        authority = self._authority_from_endpoint(endpoint)
        previous = self._active_authority
        self._active_authority = authority
        try:
            return await self._reconcile_endpoint_authorized(endpoint)
        finally:
            self._active_authority = previous

    async def _reconcile_endpoint_authorized(
        self, endpoint: dict[str, Any]
    ) -> dict[str, Any] | None:
        """Implementation for one endpoint after authority has been installed."""
        name = endpoint["endpoint_name"]
        desired_state = endpoint.get("desired_state", "deploying")
        target_regions = endpoint.get("target_regions", [])
        spec = endpoint.get("spec", {})
        ns = endpoint.get("namespace", self.namespace)

        lifecycle_value = endpoint.get("lifecycle_id")
        lifecycle_id = (
            lifecycle_value if isinstance(lifecycle_value, str) and lifecycle_value else None
        )

        if desired_state == "deleted":
            generation = endpoint.get("deletion_generation")
            deletion_regions = endpoint.get("deletion_regions")
            if not isinstance(generation, str) or not isinstance(deletion_regions, list):
                # Upgrade a legacy or interrupted delete transition atomically;
                # the next pass sees the persisted immutable snapshot.
                self.store.update_desired_state(
                    name,
                    "deleted",
                    expected_lifecycle_id=lifecycle_id,
                )
                return {"action": "initialize_deletion", "endpoint": name}
            if self.region not in deletion_regions:
                return None
            if self._cleanup_ack_is_terminal(endpoint, self.region):
                # Durable current-generation acknowledgement makes completed
                # non-target work quiescent: no Kubernetes reads and no DDB write.
                return None
            return self._reconcile_deleted(endpoint, ns, spec if isinstance(spec, dict) else None)

        if self.region not in target_regions:
            cleanup_regions = endpoint.get("cleanup_regions")
            if not isinstance(cleanup_regions, list) or self.region not in cleanup_regions:
                return None
            if self._cleanup_ack_is_terminal(endpoint, self.region):
                return None
            cleanup = self._delete_resources(
                name,
                ns,
                spec if isinstance(spec, dict) else None,
                expected_lifecycle_id=lifecycle_id,
            )
            state, written = self._record_cleanup_observation(endpoint, cleanup)
            return {
                "action": "cleanup",
                "endpoint": name,
                "reason": "region_removed",
                "cleanup_complete": state == "deleted" and written,
            }

        if desired_state in ("deploying", "running"):
            if not isinstance(spec, dict):
                error = "endpoint spec must be a mapping"
            elif "mooncake" in spec and "canary" in spec:
                error = "endpoint spec cannot combine 'mooncake' and 'canary' blocks"
            else:
                return await self._reconcile_running(name, ns, spec, endpoint)
            logger.error("Rejecting invalid endpoint %s: %s", name, error)
            self.store.update_region_status(
                name,
                self.region,
                "failed",
                error=error,
                **self._status_write_conditions(endpoint),
            )
            return {"action": "reject", "endpoint": name, "reason": "invalid_spec"}
        if desired_state == "stopped":
            return self._reconcile_stopped(
                name,
                ns,
                spec if isinstance(spec, dict) else None,
                endpoint,
            )
        return None

    async def _reconcile_running(
        self,
        name: str,
        namespace: str,
        spec: dict[str, Any],
        endpoint: dict[str, Any],
    ) -> dict[str, Any] | None:
        """Ensure the endpoint is running with the correct spec."""
        status_conditions = self._status_write_conditions(endpoint)
        # Specs carrying a ``mooncake`` block take the disaggregated path. The
        # branch returns ``None`` when no such block is present, so a plain
        # endpoint falls through to the single-Deployment path below unchanged.
        mooncake_action = await self._reconcile_mooncake(name, namespace, spec, endpoint)
        if mooncake_action is not None:
            return mooncake_action

        deployment = self._get_deployment(name, namespace)
        configured_replicas = spec.get("replicas", 1)
        raw_autoscaling = spec.get("autoscaling", {})
        autoscaling = raw_autoscaling if isinstance(raw_autoscaling, dict) else {}
        autoscaling_enabled = bool(autoscaling.get("enabled"))

        if deployment is None:
            # A missing Deployment is still an ownership handoff boundary. Do
            # not recreate it until every obsolete HPA/KEDA owner is absent.
            if autoscaling_enabled:
                ownership = self._reconcile_classic_autoscaler(
                    name,
                    namespace,
                    spec,
                    apply_desired=False,
                )
            else:
                ownership = self._delete_autoscalers(
                    (name,),
                    (name, f"keda-hpa-{name}"),
                    namespace,
                )
            if not ownership.complete:
                ownership_status_kwargs: dict[str, Any] = {}
                if ownership.error_message:
                    ownership_status_kwargs["error"] = ownership.error_message
                self.store.update_region_status(
                    name,
                    self.region,
                    "updating",
                    replicas_ready=0,
                    replicas_desired=0,
                    **status_conditions,
                    **ownership_status_kwargs,
                )
                return {
                    "action": "reconcile_autoscaler",
                    "endpoint": name,
                    "cleanup_complete": False,
                }

            logger.info("Creating endpoint %s in %s", name, self.region)
            self._create_deployment(name, namespace, spec)
            self._create_service(name, namespace, spec)
            if autoscaling_enabled:
                self._create_or_update_hpa(name, namespace, spec)
            self.store.update_region_status(
                name,
                self.region,
                "creating",
                replicas_desired=(
                    int(autoscaling.get("min_replicas", 1))
                    if autoscaling_enabled
                    else configured_replicas
                ),
                **status_conditions,
            )
            return {"action": "create", "endpoint": name}

        # Deployment exists — ensure its Service exists. Public traffic follows
        # ``gco-system/gco-gateway``'s shared ``/inference`` HTTPRoute to
        # ``gco-system/inference-proxy``, which then reaches this endpoint's
        # ClusterIP Service.
        self._ensure_service(name, namespace, spec)

        # Once enabled, HPA/KEDA is the sole owner of Deployment
        # ``spec.replicas``; the static endpoint count must never fight it.
        # Controller creation/handoff runs after capturing live replica status
        # below so a pending ownership transition can report useful progress.

        observed_desired = getattr(deployment.spec, "replicas", None)
        live_desired_replicas = (
            int(observed_desired) if observed_desired is not None else configured_replicas
        )
        current_replicas = live_desired_replicas
        ready_replicas = int(getattr(deployment.status, "ready_replicas", 0) or 0)
        # During enabled -> disabled handoff the old owner still controls this
        # observed value. Report it until static reconciliation actually takes
        # ownership, rather than publishing an aspirational configured count.
        status_desired_replicas = live_desired_replicas
        readiness_floor = (
            int(autoscaling.get("min_replicas", 1)) if autoscaling_enabled else configured_replicas
        )

        self._check_health_watchdog(
            name,
            namespace,
            ready_replicas,
            status_desired_replicas,
            spec,
            endpoint,
        )

        autoscaler_cleanup: ResourceCleanupResult
        if autoscaling_enabled:
            # Reconcile on every existing-Deployment pass to repair partial
            # creation and configuration drift. Ownership handoffs remain
            # updating until the obsolete controller is actually absent.
            autoscaler_cleanup = self._reconcile_classic_autoscaler(name, namespace, spec)
        else:
            # Static ownership is explicit even when the autoscaling block was
            # removed entirely: stale HPA/KEDA objects must never regain count.
            autoscaler_cleanup = self._delete_autoscalers(
                (name,),
                (name, f"keda-hpa-{name}"),
                namespace,
            )

        if not autoscaler_cleanup.complete:
            status_kwargs: dict[str, Any] = {}
            if autoscaler_cleanup.error_message:
                status_kwargs["error"] = autoscaler_cleanup.error_message
            self.store.update_region_status(
                name,
                self.region,
                "updating",
                replicas_ready=ready_replicas,
                replicas_desired=status_desired_replicas,
                **status_conditions,
                **status_kwargs,
            )
            return {
                "action": "reconcile_autoscaler",
                "endpoint": name,
                "cleanup_complete": False,
            }

        if not autoscaling_enabled and current_replicas != configured_replicas:
            logger.info(
                "Scaling endpoint %s: %d → %d replicas",
                name,
                current_replicas,
                configured_replicas,
            )
            self._scale_deployment(name, namespace, configured_replicas)
            self.store.update_region_status(
                name,
                self.region,
                "updating",
                replicas_ready=ready_replicas,
                replicas_desired=configured_replicas,
                **status_conditions,
            )
            return {"action": "scale", "endpoint": name, "replicas": configured_replicas}

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
                replicas_desired=status_desired_replicas,
                **status_conditions,
            )
            return {"action": "update_image", "endpoint": name, "image": desired_image}

        # Reconcile canary first and publish only observed readiness. The
        # authenticated proxy will not sample canary traffic until this exact
        # region reports the matching image fully Ready.
        canary = spec.get("canary")
        canary_status = None
        if isinstance(canary, dict):
            canary_status = self._reconcile_canary(name, namespace, spec, canary, endpoint)
        else:
            self._cleanup_canary(name, namespace)

        # For an autoscaled endpoint, readiness means the configured minimum
        # serving capacity is available. During ordinary scale-out the live HPA
        # target can temporarily exceed Ready pods without making a healthy
        # endpoint flap back to "creating". Keep reporting that live target so
        # status and watchdog diagnostics remain truthful.
        state = "running" if ready_replicas >= readiness_floor else "creating"
        self.store.update_region_status(
            name,
            self.region,
            state,
            replicas_ready=ready_replicas,
            replicas_desired=status_desired_replicas,
            extra={"canary": canary_status} if canary_status is not None else None,
            **status_conditions,
        )

        # Promote desired state only from live local readiness plus explicit
        # running observations for every *other* target region. The endpoint
        # object may contain a stale local region_status from before this pass.
        if state == "running" and endpoint.get("desired_state") == "deploying":
            stored_statuses = endpoint.get("region_status", {})
            target_regions = endpoint.get("target_regions", [])
            all_running = bool(target_regions)
            for target_region in target_regions:
                if target_region == self.region:
                    continue
                target_status = (
                    stored_statuses.get(target_region, {})
                    if isinstance(stored_statuses, dict)
                    else {}
                )
                if not isinstance(target_status, dict) or target_status.get("state") != "running":
                    all_running = False
                    break
            if all_running:
                lifecycle_id = endpoint.get("lifecycle_id")
                if isinstance(lifecycle_id, str) and lifecycle_id:
                    self.store.update_desired_state(
                        name,
                        "running",
                        expected_lifecycle_id=lifecycle_id,
                        expected_desired_state="deploying",
                    )

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
                annotations=self._provenance_annotations(),
            ),
            data={"mooncake.json": json.dumps(cfg, sort_keys=True)},
        )
        self._assert_mutation_authority()
        try:
            self.core_v1.create_namespaced_config_map(
                ns, config_map, _request_timeout=self._k8s_timeout
            )
            self._confirm_created_resource(
                kind="configmap",
                resource_name=cm_name,
                read_resource=partial(
                    self.core_v1.read_namespaced_config_map,
                    cm_name,
                    ns,
                    _request_timeout=self._k8s_timeout,
                ),
                delete_resource=partial(
                    self.core_v1.delete_namespaced_config_map,
                    cm_name,
                    ns,
                    _request_timeout=self._k8s_timeout,
                ),
            )
            logger.info("Created mooncake config map %s/%s", ns, cm_name)
        except ApiException as error:
            if error.status != 409:
                raise
            existing = self.core_v1.read_namespaced_config_map(
                cm_name, ns, _request_timeout=self._k8s_timeout
            )
            existing = self._authorize_resource(
                existing,
                kind="configmap",
                resource_name=cm_name,
                patch_metadata=partial(
                    self.core_v1.patch_namespaced_config_map,
                    cm_name,
                    ns,
                    _request_timeout=self._k8s_timeout,
                ),
                read_resource=lambda: self.core_v1.read_namespaced_config_map(
                    cm_name, ns, _request_timeout=self._k8s_timeout
                ),
                delete_resource=partial(
                    self.core_v1.delete_namespaced_config_map,
                    cm_name,
                    ns,
                    _request_timeout=self._k8s_timeout,
                ),
            )
            _metadata, _annotations, _uid, resource_version = self._object_metadata(existing)
            config_map.metadata.resource_version = resource_version
            self._assert_mutation_authority()
            self.core_v1.patch_namespaced_config_map(
                cm_name, ns, config_map, _request_timeout=self._k8s_timeout
            )
            logger.info("Updated mooncake config map %s/%s", ns, cm_name)

    def _ensure_role_deployment(
        self, name: str, ns: str, spec: dict[str, Any], role: str
    ) -> tuple[int, int, bool]:
        """Ensure one role target has its static or restart-seed capacity.

        Existing autoscaled targets normally retain controller ownership, but a
        manually stopped zero target is seeded to the role minimum before its
        autoscaler is reapplied. Static roles always converge to topology.
        """
        mooncake = spec.get("mooncake") or {}
        deploy_name = name if role == "single" else f"{name}-{role}"
        desired = self._replica_count_for_role(mooncake, role)

        deployment = self._get_deployment(deploy_name, ns)
        if deployment is None:
            self._create_role_deployment(name, ns, spec, role)
            return 0, desired, False

        autoscaling = mooncake.get("autoscaling") or {}
        role_autoscaling = autoscaling.get(role)
        autoscaled = (
            bool(autoscaling.get("enabled"))
            and role in ("prefill", "decode")
            and isinstance(role_autoscaling, dict)
        )
        current = deployment.spec.replicas or 0
        restarted = autoscaled and current == 0
        if (not autoscaled and current != desired) or restarted:
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
        return ready, desired, restarted

    def _report_role_status(
        self,
        name: str,
        ns: str,
        mooncake: dict[str, Any],
        region_services: dict[str, Any],
        endpoint: dict[str, Any] | None = None,
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
                configured_floor = self._replica_count_for_role(mooncake, role)
                dep = self._get_deployment(deploy_name, ns)
                status = getattr(dep, "status", None) if dep else None
                ready = int(getattr(status, "ready_replicas", 0) or 0) if status else 0
                deployment_spec = getattr(dep, "spec", None) if dep else None
                observed_desired = getattr(deployment_spec, "replicas", None)
                desired = (
                    int(observed_desired) if observed_desired is not None else configured_floor
                )
                roles_block[role] = {"ready": ready, "desired": desired}
                total_ready += ready
                total_desired += desired
                if ready < configured_floor:
                    all_ready = False
            extra["roles"] = roles_block
        else:
            # Store mode runs a single kv_both Deployment under the endpoint name.
            configured_floor = self._replica_count_for_role(mooncake, "single")
            dep = self._get_deployment(name, ns)
            status = getattr(dep, "status", None) if dep else None
            ready = int(getattr(status, "ready_replicas", 0) or 0) if status else 0
            deployment_spec = getattr(dep, "spec", None) if dep else None
            observed_desired = getattr(deployment_spec, "replicas", None)
            desired = int(observed_desired) if observed_desired is not None else configured_floor
            total_ready += ready
            total_desired += desired
            if ready < configured_floor:
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
        status_conditions = self._status_write_conditions(endpoint or {})
        self.store.update_region_status(
            name,
            self.region,
            state,
            replicas_ready=total_ready,
            replicas_desired=total_desired,
            extra=extra or None,
            **status_conditions,
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
        configured replica count and one internal ClusterIP Service, with no role
        split, proxy, autoscaler, shared-master dependency, endpoint Ingress,
        Gateway, or HTTPRoute. The shared platform route remains unchanged.

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
        7. Front disaggregated and both modes with the proxy and its internal
           ClusterIP Service; give store mode an internal ClusterIP Service.
           Public traffic remains on the shared ``gco-system/gco-gateway``
           HTTPRoute from ``/inference`` to ``gco-system/inference-proxy``.
        8. Write the role-keyed region status.

        Returns:
            An action record describing what the pass did, or ``None`` when the
            spec carries no ``mooncake`` block.
        """
        mooncake = spec.get("mooncake")
        if not mooncake:
            return None

        status_conditions = self._status_write_conditions(endpoint)
        mode = mooncake.get("mode")

        # Step 1: resolve in-region addresses. A store without an own-region
        # master is left untouched and reported as still coming up.
        services = self._resolve_region_services(name, mooncake)
        if services.render_skipped:
            self.store.update_region_status(
                name,
                self.region,
                "creating",
                error=services.error,
                **status_conditions,
            )
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
                name,
                self.region,
                scope.state or "failed",
                error=scope.error,
                **status_conditions,
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
                    name,
                    self.region,
                    gate.state or "creating",
                    error=gate.error,
                    **status_conditions,
                )
                return {
                    "action": "reconcile_mooncake",
                    "endpoint": name,
                    "deferred": "master_not_ready",
                }

        # Step 4: shared transport ConfigMap, applied once before role pods.
        cfg = render_mooncake_config(mooncake, region_services)
        self._ensure_mooncake_configmap(name, ns, cfg)

        # Steps 5-6: converge ownership before creating/scaling any role
        # Deployment. This is the same recreate/handoff barrier used by classic
        # endpoints, extended to prefill/decode and stale topology roles.
        desired_roles = self._desired_roles(mode)
        ownership_results = [
            self._delete_autoscalers(
                (name,),
                (name, f"keda-hpa-{name}"),
                ns,
            )
        ]
        for role in ("prefill", "decode"):
            role_name = f"{name}-{role}"
            if role in desired_roles:
                ownership_results.append(
                    self._reconcile_role_autoscaler(
                        name,
                        ns,
                        spec,
                        role,
                        apply_desired=False,
                    )
                )
            else:
                ownership_results.append(
                    self._delete_autoscalers(
                        (role_name,),
                        (role_name, f"keda-hpa-{role_name}"),
                        ns,
                    )
                )
        ownership = self._merge_cleanup_results(*ownership_results)
        if not ownership.complete:
            ready_total = 0
            desired_total = 0
            for role in desired_roles:
                deploy_name = name if role == "single" else f"{name}-{role}"
                deployment = self._get_deployment(deploy_name, ns)
                deployment_status = getattr(deployment, "status", None)
                deployment_spec = getattr(deployment, "spec", None)
                ready_total += int(getattr(deployment_status, "ready_replicas", 0) or 0)
                desired_total += int(getattr(deployment_spec, "replicas", 0) or 0)
            status_kwargs: dict[str, Any] = {}
            if ownership.error_message:
                status_kwargs["error"] = ownership.error_message
            self.store.update_region_status(
                name,
                self.region,
                "updating",
                replicas_ready=ready_total,
                replicas_desired=desired_total,
                **status_conditions,
                **status_kwargs,
            )
            return {
                "action": "reconcile_mooncake_autoscaler",
                "endpoint": name,
                "cleanup_complete": False,
            }

        role_ready_total = 0
        role_desired_total = 0
        for role in desired_roles:
            ready, desired, _restarted = self._ensure_role_deployment(name, ns, spec, role)
            role_ready_total += ready
            role_desired_total += desired

        owner_verifications: list[ResourceCleanupResult] = []
        for role in ("prefill", "decode"):
            role_config = self._role_autoscaling_config(spec, role)
            if role not in desired_roles or role_config is None:
                continue
            self._create_role_hpa(name, ns, spec, role)
            target_name = f"{name}-{role}"
            metrics_config = role_config.get("metrics", [{"type": "cpu", "target": 70}])
            hpa_name = (
                f"keda-hpa-{target_name}"
                if self._metrics_require_keda(metrics_config)
                else target_name
            )
            owner_verifications.append(self._verify_hpa_owner(hpa_name, ns, target_name))
        verified_owners = self._merge_cleanup_results(*owner_verifications)
        if not verified_owners.complete:
            verification_status_kwargs: dict[str, Any] = {}
            if verified_owners.error_message:
                verification_status_kwargs["error"] = verified_owners.error_message
            self.store.update_region_status(
                name,
                self.region,
                "updating",
                replicas_ready=role_ready_total,
                replicas_desired=role_desired_total,
                **status_conditions,
                **verification_status_kwargs,
            )
            return {
                "action": "reconcile_mooncake_autoscaler",
                "endpoint": name,
                "cleanup_complete": False,
            }

        # Step 7: front-end. Disaggregated and both run behind the proxy; store
        # exposes its single Deployment directly.
        if mode in ("disaggregated", "both"):
            # Per-role Services so the proxy can address prefill and decode by
            # stable in-cluster DNS. Routing through a Service means kube-proxy
            # load-balances across only the Ready pods of each role, which is
            # what gives the proxy ready-only decode routing for free.
            role_port = spec.get("port", 8000)
            for role in desired_roles:
                self._create_role_service(name, ns, role, role_port)
            try:
                self._create_pd_proxy(name, ns, spec, endpoint)
            except AdminApiKeySecretError as e:
                logger.error("Proxy for endpoint %s in %s not started: %s", name, ns, e)
                self.store.update_region_status(
                    name,
                    self.region,
                    "failed",
                    error=str(e),
                    **status_conditions,
                )
                return {
                    "action": "reconcile_mooncake",
                    "endpoint": name,
                    "failed": "admin_api_key",
                }
        else:
            self._create_service(name, ns, spec)

        # Step 8: role-keyed status.
        state = self._report_role_status(
            name,
            ns,
            mooncake,
            region_services,
            endpoint,
        )
        return {"action": "reconcile_mooncake", "endpoint": name, "state": state}

    def _reconcile_stopped(
        self,
        name: str,
        namespace: str,
        spec: dict[str, Any] | None = None,
        endpoint: dict[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        """Remove every possible autoscaler owner before scaling all roles to zero."""
        mooncake_endpoint = isinstance(spec, dict) and isinstance(spec.get("mooncake"), dict)
        self._unready_since.pop(name, None)
        self._master_deferral_since.pop(name, None)
        status_conditions = self._status_write_conditions(endpoint or {})
        role_names = (name, f"{name}-prefill", f"{name}-decode")
        cleanup = self._delete_autoscalers(
            role_names,
            (
                name,
                f"{name}-prefill",
                f"{name}-decode",
                f"keda-hpa-{name}",
                f"keda-hpa-{name}-prefill",
                f"keda-hpa-{name}-decode",
            ),
            namespace,
        )
        deployment_names = (*role_names, f"{name}-proxy") if mooncake_endpoint else (name,)
        deployments = {
            deployment_name: self._get_deployment(deployment_name, namespace)
            for deployment_name in deployment_names
        }
        ready_replicas = sum(
            int(getattr(getattr(deployment, "status", None), "ready_replicas", 0) or 0)
            for deployment in deployments.values()
            if deployment is not None
        )
        desired_replicas = sum(
            int(getattr(getattr(deployment, "spec", None), "replicas", 0) or 0)
            for deployment in deployments.values()
            if deployment is not None
        )
        if not cleanup.complete:
            status_kwargs: dict[str, Any] = {}
            if cleanup.error_message:
                status_kwargs["error"] = cleanup.error_message
            self.store.update_region_status(
                name,
                self.region,
                "stopping",
                replicas_ready=ready_replicas,
                replicas_desired=desired_replicas,
                **status_conditions,
                **status_kwargs,
            )
            return {"action": "stop", "endpoint": name, "cleanup_complete": False}

        scaled = False
        for deployment_name, deployment in deployments.items():
            if deployment is None:
                continue
            current_replicas = int(getattr(getattr(deployment, "spec", None), "replicas", 0) or 0)
            if current_replicas <= 0:
                continue
            logger.info("Stopping endpoint role %s (scaling to 0)", deployment_name)
            self._scale_deployment(deployment_name, namespace, 0)
            scaled = True

        self.store.update_region_status(
            name,
            self.region,
            "stopped",
            replicas_ready=0,
            replicas_desired=0,
            **status_conditions,
        )
        if scaled:
            return {"action": "stop", "endpoint": name, "cleanup_complete": True}
        return None

    def _reconcile_deleted(
        self,
        endpoint: dict[str, Any],
        namespace: str,
        spec: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Converge all parents and generated children to stable absence."""
        name = endpoint["endpoint_name"]
        lifecycle_id = endpoint.get("lifecycle_id")
        if not isinstance(lifecycle_id, str) or not lifecycle_id:
            raise RuntimeError("Endpoint deletion requires an immutable lifecycle id")
        self._unready_since.pop(name, None)
        self._master_deferral_since.pop(name, None)
        logger.info("Reconciling deletion of endpoint %s from %s", name, self.region)
        cleanup = self._delete_resources(
            name,
            namespace,
            spec,
            expected_lifecycle_id=lifecycle_id,
        )
        state, written = self._record_cleanup_observation(endpoint, cleanup)
        return {
            "action": "delete",
            "endpoint": name,
            "cleanup_complete": state == "deleted" and written,
        }

    # ------------------------------------------------------------------
    # Kubernetes resource management
    # ------------------------------------------------------------------

    def _deployment_exists(self, name: str, namespace: str) -> bool:
        return self._get_deployment(name, namespace) is not None

    def _get_deployment(self, name: str, namespace: str) -> V1Deployment | None:
        try:
            deployment = self.apps_v1.read_namespaced_deployment(
                name, namespace, _request_timeout=self._k8s_timeout
            )
        except ApiException as error:
            if error.status == 404:
                return None
            raise
        return self._authorize_resource(
            deployment,
            kind="deployment",
            resource_name=name,
            patch_metadata=partial(
                self.apps_v1.patch_namespaced_deployment,
                name,
                namespace,
                _request_timeout=self._k8s_timeout,
            ),
            read_resource=lambda: self.apps_v1.read_namespaced_deployment(
                name, namespace, _request_timeout=self._k8s_timeout
            ),
            delete_resource=partial(
                self.apps_v1.delete_namespaced_deployment,
                name,
                namespace,
                _request_timeout=self._k8s_timeout,
            ),
        )

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

        # The store needs an own-region master. It is a fixed in-cluster Service
        # the monitor itself provisions per region (mooncake-master:50051), so
        # when no override is set in the environment, default to that Service
        # rather than deferring — the address is known by construction. An
        # operator may still override it via MOONCAKE_MASTER_ADDRESS.
        if store_enabled and not master_address:
            master_address = f"{MOONCAKE_MASTER_SERVICE}:{MOONCAKE_MASTER_RPC_PORT}"

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

        param_name = f"{_regional_shared_ssm_parameter_prefix()}/name"
        try:
            bucket = get_ssm_parameter_optional(param_name, region=self.region)
            return bucket if isinstance(bucket, str) and bucket else None
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
        """Return the address's explicit AWS region or safe local classification.

        AWS region tokens embedded in the host are authoritative. Bare Service
        names and Kubernetes ``.svc`` names are local by construction. Any
        other host without a region token is external and ambiguous, so it is
        classified as ``"unknown"`` and rejected by regional-scope checks.
        """
        candidate = (address or "").strip()
        if not candidate:
            return "unknown"

        try:
            parsed = urlsplit(candidate if "://" in candidate else f"//{candidate}")
            host = (parsed.hostname or "").rstrip(".").lower()
        except ValueError:
            return "unknown"
        if not host:
            return "unknown"

        match = _REGION_TOKEN_PATTERN.search(host)
        if match:
            return match.group(0)
        if "." not in host or host.endswith((".svc", ".svc.cluster.local")):
            return self.region
        return "unknown"

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
                addresses.append(f"{name}-{role}.{ns}.svc.cluster.local")

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

        resolved_regions = [(address, self._region_of_address(address)) for address in ordered]
        out_of_region = [
            (address, region) for address, region in resolved_regions if region != self.region
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
                        automount_service_account_token=False,
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
        self._assert_mutation_authority()
        try:
            self.core_v1.create_namespaced_service(ns, service, _request_timeout=self._k8s_timeout)
            logger.info("Created shared mooncake master service in %s", ns)
        except ApiException as e:
            if e.status == 409:
                logger.info("Shared mooncake master service already exists in %s", ns)
            else:
                raise

        self._assert_mutation_authority()
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
        four widening allow rules with create-if-absent semantics:

        - ``allow-inference-internal`` — managed inference pods exchange TCP
          traffic and may reach the shared master's two fixed ports. This
          permits proxy-to-role serving and bootstrap traffic while excluding
          unselected sources such as the ALB.
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
        master_peer = [client.V1NetworkPolicyPeer(pod_selector=master_selector)]
        all_tcp = [client.V1NetworkPolicyPort(protocol="TCP")]

        policies = [
            (
                NETWORK_POLICY_INFERENCE_INTERNAL,
                client.V1NetworkPolicy(
                    metadata=client.V1ObjectMeta(
                        name=NETWORK_POLICY_INFERENCE_INTERNAL, namespace=ns, labels=labels
                    ),
                    spec=client.V1NetworkPolicySpec(
                        pod_selector=inference_selector,
                        policy_types=["Ingress", "Egress"],
                        ingress=[
                            client.V1NetworkPolicyIngressRule(
                                _from=inference_peer,
                                ports=all_tcp,
                            )
                        ],
                        egress=[
                            client.V1NetworkPolicyEgressRule(
                                to=inference_peer,
                                ports=all_tcp,
                            ),
                            client.V1NetworkPolicyEgressRule(
                                to=master_peer,
                                ports=[
                                    client.V1NetworkPolicyPort(
                                        protocol="TCP", port=MOONCAKE_MASTER_RPC_PORT
                                    ),
                                    client.V1NetworkPolicyPort(
                                        protocol="TCP", port=MOONCAKE_METADATA_PORT
                                    ),
                                ],
                            ),
                        ],
                    ),
                ),
            ),
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
            self._assert_mutation_authority()
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
        self._assert_mutation_authority()
        self.apps_v1.create_namespaced_deployment(
            namespace, deployment, _request_timeout=self._k8s_timeout
        )
        self._confirm_created_resource(
            kind="deployment",
            resource_name=name,
            read_resource=partial(
                self.apps_v1.read_namespaced_deployment,
                name,
                namespace,
                _request_timeout=self._k8s_timeout,
            ),
            delete_resource=partial(
                self.apps_v1.delete_namespaced_deployment,
                name,
                namespace,
                _request_timeout=self._k8s_timeout,
            ),
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

        # Runtime behavior is persisted explicitly by callers that need a
        # strict adapter. Legacy endpoints without the field retain vLLM image
        # detection, but TGI is never given vLLM's unsupported --root-path:
        # the authenticated platform proxy strips /inference/{name} before
        # forwarding /health, /generate, or /info to the model Service.
        serving_prefix = f"/inference/{name}"
        runtime_framework = spec.get("framework")
        if runtime_framework not in ("vllm", "tgi"):
            image_lower = image.lower()
            if "vllm" in image_lower:
                runtime_framework = "vllm"
            elif "text-generation-inference" in image_lower or "/tgi" in image_lower:
                runtime_framework = "tgi"
            else:
                runtime_framework = None
        if not command and runtime_framework == "vllm":
            if args:
                if "--root-path" not in args:
                    args = list(args) + ["--root-path", serving_prefix]
            else:
                args = ["--root-path", serving_prefix]

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

        # Sync directly with literal argv so a model URI can never become
        # shell syntax. ``aws s3 sync`` avoids retransferring unchanged
        # objects on reruns while also repairing partial downloads.
        if model_source and model_source.startswith("s3://"):
            model_dest = f"/models/{name}"
            init_containers.append(
                client.V1Container(
                    name="model-sync",
                    image=AWS_CLI_IMAGE,
                    command=["aws"],
                    args=["s3", "sync", model_source, model_dest, "--quiet"],
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
        probe_health = f"{serving_prefix}{health_path}" if uses_root_path else health_path

        container = client.V1Container(
            name="inference",
            image=image,
            ports=[client.V1ContainerPort(container_port=port)],
            env=container_env if container_env else None,
            resources=resource_reqs,
            volume_mounts=volume_mounts if volume_mounts else None,
            command=command,
            args=args,
            startup_probe=(
                client.V1Probe(
                    http_get=client.V1HTTPGetAction(path=health_path, port=port),
                    period_seconds=15,
                    failure_threshold=80,
                )
                if runtime_framework == "tgi"
                else None
            ),
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
                annotations=self._provenance_annotations(),
            ),
            spec=client.V1DeploymentSpec(
                replicas=replicas,
                selector=client.V1LabelSelector(
                    match_labels={"app": app_label},
                ),
                template=client.V1PodTemplateSpec(
                    metadata=client.V1ObjectMeta(
                        labels=dict(labels),
                        annotations=self._provenance_annotations(),
                    ),
                    spec=client.V1PodSpec(
                        service_account_name="gco-service-account",
                        automount_service_account_token=False,
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

        self._assert_mutation_authority()
        self.apps_v1.create_namespaced_deployment(
            ns, deployment, _request_timeout=self._k8s_timeout
        )
        self._confirm_created_resource(
            kind="deployment",
            resource_name=deploy_name,
            read_resource=partial(
                self.apps_v1.read_namespaced_deployment,
                deploy_name,
                ns,
                _request_timeout=self._k8s_timeout,
            ),
            delete_resource=partial(
                self.apps_v1.delete_namespaced_deployment,
                deploy_name,
                ns,
                _request_timeout=self._k8s_timeout,
            ),
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

    def _ensure_admin_api_key_secret(
        self,
        name: str,
        proxy: dict[str, Any],
        ns: str,
        lifecycle_id: str,
    ) -> str:
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
        return self._provision_admin_api_key_secret(f"{name}-admin", ns, lifecycle_id)

    def _provision_admin_api_key_secret(
        self,
        secret_name: str,
        ns: str,
        lifecycle_id: str,
    ) -> str:
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
            existing = self.core_v1.read_namespaced_secret(
                secret_name,
                ns,
                _request_timeout=self._k8s_timeout,
            )
        except ApiException as e:
            if e.status != 404:
                raise
        else:
            existing = self._authorize_resource(
                existing,
                kind="secret",
                resource_name=secret_name,
                patch_metadata=partial(
                    self.core_v1.patch_namespaced_secret,
                    secret_name,
                    ns,
                    _request_timeout=self._k8s_timeout,
                ),
                read_resource=lambda: self.core_v1.read_namespaced_secret(
                    secret_name, ns, _request_timeout=self._k8s_timeout
                ),
                delete_resource=partial(
                    self.core_v1.delete_namespaced_secret,
                    secret_name,
                    ns,
                    _request_timeout=self._k8s_timeout,
                ),
            )
            self._require_owned_admin_secret(
                existing,
                secret_name,
                ns,
                lifecycle_id,
            )
            return secret_name

        ownership = self._generated_admin_secret_labels(secret_name)
        annotations = self._provenance_annotations() or {_LIFECYCLE_ANNOTATION: lifecycle_id}
        secret = client.V1Secret(
            metadata=client.V1ObjectMeta(
                name=secret_name,
                namespace=ns,
                labels=ownership,
                annotations=annotations,
            ),
            string_data={ADMIN_API_KEY_SECRET_DATA_KEY: secrets.token_hex(32)},
            type="Opaque",
        )
        # The two logger.info calls below carry a bare `# nosemgrep`: the
        # logger-credential-disclosure rule matches the literal word "Secret" in
        # the message, but only the Secret's name and namespace (%s/%s) are
        # logged here — never the generated key value set above in string_data.
        self._assert_mutation_authority()
        try:
            self.core_v1.create_namespaced_secret(ns, secret, _request_timeout=self._k8s_timeout)
            self._confirm_created_resource(
                kind="secret",
                resource_name=secret_name,
                read_resource=partial(
                    self.core_v1.read_namespaced_secret,
                    secret_name,
                    ns,
                    _request_timeout=self._k8s_timeout,
                ),
                delete_resource=partial(
                    self.core_v1.delete_namespaced_secret,
                    secret_name,
                    ns,
                    _request_timeout=self._k8s_timeout,
                ),
            )
            logger.info("Provisioned proxy admin-key Secret %s/%s", ns, secret_name)  # nosemgrep
        except ApiException as e:
            if e.status != 409:
                raise
            existing = self.core_v1.read_namespaced_secret(
                secret_name,
                ns,
                _request_timeout=self._k8s_timeout,
            )
            existing = self._authorize_resource(
                existing,
                kind="secret",
                resource_name=secret_name,
                patch_metadata=partial(
                    self.core_v1.patch_namespaced_secret,
                    secret_name,
                    ns,
                    _request_timeout=self._k8s_timeout,
                ),
                read_resource=lambda: self.core_v1.read_namespaced_secret(
                    secret_name, ns, _request_timeout=self._k8s_timeout
                ),
                delete_resource=partial(
                    self.core_v1.delete_namespaced_secret,
                    secret_name,
                    ns,
                    _request_timeout=self._k8s_timeout,
                ),
            )
            try:
                self._require_owned_admin_secret(
                    existing,
                    secret_name,
                    ns,
                    lifecycle_id,
                )
            except AdminApiKeySecretError as error:
                raise AdminApiKeySecretError(
                    secret_name,
                    "a concurrent conventional Secret lacks matching lifecycle provenance",
                ) from error
            logger.info("Proxy admin-key Secret %s/%s exists", ns, secret_name)  # nosemgrep
        return secret_name

    def _create_pd_proxy(
        self, name: str, ns: str, spec: dict[str, Any], endpoint: dict[str, Any]
    ) -> None:
        """Materialize the prefill-decode proxy front for a disaggregated endpoint.

        Disaggregated and ``both`` modes are fronted by a lightweight proxy that
        runs the residency check and dispatches each request to the prefill and
        decode pods. It materializes a ConfigMap, a proxy Deployment with at
        least one replica, and a Service whose selector matches only the proxy
        pods. The shared HTTPRoute attached to ``gco-system/gco-gateway`` sends
        ``/inference`` to ``gco-system/inference-proxy``; that authenticated
        platform proxy then reaches this internal ClusterIP Service.
        Endpoint-specific Ingresses are removed as an unsafe legacy path.

        Before those resources are created, a user-named
        ``mooncake.proxy.admin_api_key_secret`` is verified to contain a usable
        ``ADMIN_API_KEY``. When no Secret is named, the monitor auto-provisions
        a generated ``{name}-admin`` Secret instead. A missing or empty named
        Secret rejects the proxy; the key itself reaches the container only as
        a Secret reference at pod start and is never written to the spec or a
        command argument.

        Creation is idempotent at the API boundary: an already-present Deployment
        or ClusterIP Service is left in place, and historical direct Ingresses are
        deleted if present. No endpoint Gateway or HTTPRoute is created.

        Args:
            name: The endpoint name.
            ns: The namespace to materialize into.
            spec: The endpoint spec; ``spec["mooncake"]`` supplies the proxy
                image and behavior.
            endpoint: The endpoint record. Legacy per-endpoint routing metadata
                is ignored because the shared platform HTTPRoute owns the prefix.

        Raises:
            AdminApiKeySecretError: If the admin key Secret is missing, names no
                Secret, or holds an empty ``ADMIN_API_KEY``. No proxy resource
                is created in that case.
        """
        mooncake = spec.get("mooncake") or {}
        proxy = mooncake.get("proxy") or {}
        proxy_name = f"{name}-proxy"
        lifecycle_id = endpoint.get("lifecycle_id")
        if not isinstance(lifecycle_id, str) or not lifecycle_id:
            raise AdminApiKeySecretError(
                None,
                "endpoint record has no immutable lifecycle identity",
            )

        # The proxy fronts a privileged admin path, so it never starts without a
        # usable admin key. When the spec names a Secret it must already exist
        # and be non-empty (the deployment is rejected otherwise); when it names
        # none, a per-endpoint admin-key Secret is auto-provisioned with a
        # generated key. Either way the key reaches the container only by Secret
        # reference.
        admin_secret_name = self._ensure_admin_api_key_secret(
            name,
            proxy,
            ns,
            lifecycle_id,
        )

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

        # The proxy reaches prefill and decode through their per-role Services
        # and listens on PD_PROXY_PORT for requests from the authenticated API
        # proxy. Routing via Services means only Ready role pods receive traffic.
        port = spec.get("port", 8000)
        container_env.extend(
            [
                client.V1EnvVar(name=PD_PROXY_PORT_ENV, value=str(PD_PROXY_PORT)),
                client.V1EnvVar(
                    name=PD_PROXY_PREFILL_URL_ENV, value=f"http://{name}-prefill:{port}"
                ),
                client.V1EnvVar(name=PD_PROXY_DECODE_URL_ENV, value=f"http://{name}-decode:{port}"),
            ]
        )

        # Ship the proxy program to the pod as a ConfigMap and run it from there.
        self._ensure_pd_proxy_configmap(name, ns)
        proxy_volume_name = "pd-proxy-script"

        container = client.V1Container(
            name="proxy",
            image=proxy.get("image"),
            command=["python3", PD_PROXY_SCRIPT_PATH],
            ports=[client.V1ContainerPort(container_port=PD_PROXY_PORT)],
            env=container_env if container_env else None,
            resources=client.V1ResourceRequirements(
                requests={"cpu": "250m", "memory": "256Mi"},
                limits={"cpu": "1", "memory": "1Gi"},
            ),
            volume_mounts=[
                client.V1VolumeMount(
                    name=proxy_volume_name,
                    mount_path=PD_PROXY_CONFIG_MOUNT_DIR,
                    read_only=True,
                )
            ],
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
                annotations=self._provenance_annotations(),
            ),
            spec=client.V1DeploymentSpec(
                replicas=replicas,
                selector=client.V1LabelSelector(match_labels={"app": proxy_name}),
                template=client.V1PodTemplateSpec(
                    metadata=client.V1ObjectMeta(
                        labels=dict(labels),
                        annotations=self._provenance_annotations(),
                    ),
                    spec=client.V1PodSpec(
                        service_account_name="gco-service-account",
                        automount_service_account_token=False,
                        containers=[container],
                        volumes=[
                            client.V1Volume(
                                name=proxy_volume_name,
                                config_map=client.V1ConfigMapVolumeSource(
                                    name=f"{name}-pd-proxy",
                                    default_mode=0o555,
                                ),
                            )
                        ],
                    ),
                ),
            ),
        )

        self._assert_mutation_authority()
        try:
            self.apps_v1.create_namespaced_deployment(
                ns, deployment, _request_timeout=self._k8s_timeout
            )
            self._confirm_created_resource(
                kind="deployment",
                resource_name=proxy_name,
                read_resource=partial(
                    self.apps_v1.read_namespaced_deployment,
                    proxy_name,
                    ns,
                    _request_timeout=self._k8s_timeout,
                ),
                delete_resource=partial(
                    self.apps_v1.delete_namespaced_deployment,
                    proxy_name,
                    ns,
                    _request_timeout=self._k8s_timeout,
                ),
            )
            logger.info("Created proxy deployment %s/%s", ns, proxy_name)
        except ApiException as error:
            if error.status != 409:
                raise
            existing = self._get_deployment(proxy_name, ns)
            if existing is None:
                raise ReconcileFencedError(
                    "proxy deployment disappeared during reconciliation"
                ) from error
            _metadata, _annotations, _uid, resource_version = self._object_metadata(existing)
            deployment.metadata.resource_version = resource_version
            self._assert_mutation_authority()
            self.apps_v1.patch_namespaced_deployment(
                proxy_name,
                ns,
                body=deployment,
                _request_timeout=self._k8s_timeout,
            )
            logger.info("Reconciled proxy deployment %s/%s", ns, proxy_name)

        self._create_proxy_service(proxy_name, ns)

    def _authorize_existing_service(self, name: str, namespace: str) -> Any:
        service = self.core_v1.read_namespaced_service(
            name, namespace, _request_timeout=self._k8s_timeout
        )
        return self._authorize_resource(
            service,
            kind="service",
            resource_name=name,
            patch_metadata=partial(
                self.core_v1.patch_namespaced_service,
                name,
                namespace,
                _request_timeout=self._k8s_timeout,
            ),
            read_resource=lambda: self.core_v1.read_namespaced_service(
                name, namespace, _request_timeout=self._k8s_timeout
            ),
            delete_resource=partial(
                self.core_v1.delete_namespaced_service,
                name,
                namespace,
                _request_timeout=self._k8s_timeout,
            ),
        )

    def _create_role_service(self, name: str, ns: str, role: str, port: int = 8000) -> None:
        """Create the ClusterIP Service that fronts one role's pods.

        Named ``{name}-{role}`` and selecting that role Deployment's app label,
        so the PD proxy can address prefill or decode by stable in-cluster DNS.
        Routing through a Service means kube-proxy load-balances across only the
        role's Ready pods, which is what gives the proxy ready-only decode
        routing without watching the Kubernetes API. Idempotent at the API
        boundary: an already-present Service is left in place.
        """
        deploy_name = f"{name}-{role}"
        service = client.V1Service(
            metadata=client.V1ObjectMeta(
                name=deploy_name,
                namespace=ns,
                labels={
                    "app": deploy_name,
                    "project": "gco",
                    "gco.io/type": "inference",
                    "gco.io/role": role,
                },
                annotations=self._provenance_annotations(),
            ),
            spec=client.V1ServiceSpec(
                selector={"app": deploy_name},
                ports=[client.V1ServicePort(port=port, target_port=port, protocol="TCP")],
                type="ClusterIP",
            ),
        )
        self._assert_mutation_authority()
        try:
            self.core_v1.create_namespaced_service(ns, service, _request_timeout=self._k8s_timeout)
            self._confirm_created_resource(
                kind="service",
                resource_name=deploy_name,
                read_resource=partial(
                    self.core_v1.read_namespaced_service,
                    deploy_name,
                    ns,
                    _request_timeout=self._k8s_timeout,
                ),
                delete_resource=partial(
                    self.core_v1.delete_namespaced_service,
                    deploy_name,
                    ns,
                    _request_timeout=self._k8s_timeout,
                ),
            )
            logger.info("Created role service %s/%s", ns, deploy_name)
        except ApiException as error:
            if error.status != 409:
                raise
            self._authorize_existing_service(deploy_name, ns)
            logger.info("Role service %s/%s already exists", ns, deploy_name)

    def _ensure_pd_proxy_configmap(self, name: str, ns: str) -> None:
        """Publish the PD proxy program to the pod as a ConfigMap.

        The proxy program (``mooncake_pd_proxy.py``) ships in this image
        alongside the monitor; its source is read here and mounted into the
        ``{name}-proxy`` pod, which runs it with ``python3`` from
        ``PD_PROXY_SCRIPT_PATH``. The ConfigMap is patched on conflict so the
        program tracks the running monitor build.
        """
        script = (Path(__file__).resolve().parent / PD_PROXY_SCRIPT_FILENAME).read_text(
            encoding="utf-8"
        )
        cm_name = f"{name}-pd-proxy"
        body = client.V1ConfigMap(
            metadata=client.V1ObjectMeta(
                name=cm_name,
                namespace=ns,
                labels={
                    "app": f"{name}-proxy",
                    "project": "gco",
                    "gco.io/type": "inference",
                    "gco.io/role": PD_PROXY_ROLE_LABEL,
                },
                annotations=self._provenance_annotations(),
            ),
            data={PD_PROXY_SCRIPT_FILENAME: script},
        )
        self._assert_mutation_authority()
        try:
            self.core_v1.create_namespaced_config_map(ns, body, _request_timeout=self._k8s_timeout)
            self._confirm_created_resource(
                kind="configmap",
                resource_name=cm_name,
                read_resource=partial(
                    self.core_v1.read_namespaced_config_map,
                    cm_name,
                    ns,
                    _request_timeout=self._k8s_timeout,
                ),
                delete_resource=partial(
                    self.core_v1.delete_namespaced_config_map,
                    cm_name,
                    ns,
                    _request_timeout=self._k8s_timeout,
                ),
            )
            logger.info("Created PD proxy ConfigMap %s/%s", ns, cm_name)
        except ApiException as error:
            if error.status != 409:
                raise
            existing = self.core_v1.read_namespaced_config_map(
                cm_name, ns, _request_timeout=self._k8s_timeout
            )
            existing = self._authorize_resource(
                existing,
                kind="configmap",
                resource_name=cm_name,
                patch_metadata=partial(
                    self.core_v1.patch_namespaced_config_map,
                    cm_name,
                    ns,
                    _request_timeout=self._k8s_timeout,
                ),
                read_resource=lambda: self.core_v1.read_namespaced_config_map(
                    cm_name, ns, _request_timeout=self._k8s_timeout
                ),
                delete_resource=partial(
                    self.core_v1.delete_namespaced_config_map,
                    cm_name,
                    ns,
                    _request_timeout=self._k8s_timeout,
                ),
            )
            _metadata, _annotations, _uid, resource_version = self._object_metadata(existing)
            body.metadata.resource_version = resource_version
            self._assert_mutation_authority()
            self.core_v1.patch_namespaced_config_map(
                cm_name, ns, body, _request_timeout=self._k8s_timeout
            )
            logger.info("Updated PD proxy ConfigMap %s/%s", ns, cm_name)

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
                annotations=self._provenance_annotations(),
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

        self._assert_mutation_authority()
        try:
            self.core_v1.create_namespaced_service(
                namespace, service, _request_timeout=self._k8s_timeout
            )
            self._confirm_created_resource(
                kind="service",
                resource_name=proxy_name,
                read_resource=partial(
                    self.core_v1.read_namespaced_service,
                    proxy_name,
                    namespace,
                    _request_timeout=self._k8s_timeout,
                ),
                delete_resource=partial(
                    self.core_v1.delete_namespaced_service,
                    proxy_name,
                    namespace,
                    _request_timeout=self._k8s_timeout,
                ),
            )
            logger.info("Created proxy service %s/%s", namespace, proxy_name)
        except ApiException as error:
            if error.status != 409:
                raise
            self._authorize_existing_service(proxy_name, namespace)
            logger.info("Proxy service %s/%s already exists", namespace, proxy_name)

    def _create_service(self, name: str, namespace: str, spec: dict[str, Any]) -> None:
        """Create the internal ClusterIP Service for an inference endpoint."""
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
                annotations=self._provenance_annotations(),
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

        self._assert_mutation_authority()
        try:
            self.core_v1.create_namespaced_service(
                namespace, service, _request_timeout=self._k8s_timeout
            )
            self._confirm_created_resource(
                kind="service",
                resource_name=name,
                read_resource=partial(
                    self.core_v1.read_namespaced_service,
                    name,
                    namespace,
                    _request_timeout=self._k8s_timeout,
                ),
                delete_resource=partial(
                    self.core_v1.delete_namespaced_service,
                    name,
                    namespace,
                    _request_timeout=self._k8s_timeout,
                ),
            )
            logger.info("Created service %s/%s", namespace, name)
        except ApiException as error:
            if error.status != 409:
                raise
            self._authorize_existing_service(name, namespace)
            logger.info("Service %s/%s already exists", namespace, name)

    def _ensure_service(self, name: str, namespace: str, spec: dict[str, Any]) -> None:
        """Ensure an owned endpoint Service exists, recreating it if absent."""
        try:
            self._authorize_existing_service(name, namespace)
        except ApiException as error:
            if error.status == 404:
                logger.warning("Service %s/%s missing, recreating", namespace, name)
                self._create_service(name, namespace, spec)
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
        """Track prolonged unavailability without changing shared routing.

        ``gco-system/gco-gateway`` owns the shared ``/inference`` HTTPRoute to
        ``gco-system/inference-proxy``. Individual models are reached through
        internal ClusterIP Services, so their readiness never changes the shared
        Gateway or HTTPRoute. The threshold still drives degraded-state logging.
        """
        del namespace, spec, endpoint
        if ready_replicas > 0:
            if name in self._unready_since:
                logger.info("Endpoint %s recovered", name)
                del self._unready_since[name]
            return False

        now = datetime.now(UTC)
        if name not in self._unready_since:
            self._unready_since[name] = now
            logger.warning(
                "Endpoint %s has 0/%d ready replicas, starting health watchdog timer",
                name,
                desired_replicas,
            )
            return False

        unready_duration = (now - self._unready_since[name]).total_seconds()
        threshold_exceeded = unready_duration >= self._unhealthy_threshold_seconds
        if threshold_exceeded:
            logger.warning(
                "WATCHDOG: Endpoint %s has been unavailable for %ds (threshold %ds); "
                "the authenticated proxy will return 503 until it recovers",
                name,
                int(unready_duration),
                self._unhealthy_threshold_seconds,
            )
        return threshold_exceeded

    def _scale_deployment(self, name: str, namespace: str, replicas: int) -> None:
        """Scale only the exact authorized Deployment resourceVersion."""
        deployment = self._get_deployment(name, namespace)
        if deployment is None:
            raise ReconcileFencedError(f"deployment {name} disappeared before scaling")
        _metadata, _annotations, _uid, resource_version = self._object_metadata(deployment)
        self._assert_mutation_authority()
        body: dict[str, Any] = {"spec": {"replicas": replicas}}
        if resource_version:
            body["metadata"] = {"resourceVersion": resource_version}
        self.apps_v1.patch_namespaced_deployment(
            name,
            namespace,
            body=body,
            _request_timeout=self._k8s_timeout,
        )

    def _update_deployment_image(self, name: str, namespace: str, image: str) -> None:
        """Update only the exact authorized Deployment resourceVersion."""
        deployment = self._get_deployment(name, namespace)
        if deployment is None:
            raise ReconcileFencedError(f"deployment {name} disappeared before image update")
        _metadata, _annotations, _uid, resource_version = self._object_metadata(deployment)
        self._assert_mutation_authority()
        body: dict[str, Any] = {
            "spec": {"template": {"spec": {"containers": [{"name": "inference", "image": image}]}}}
        }
        if resource_version:
            body["metadata"] = {"resourceVersion": resource_version}
        self.apps_v1.patch_namespaced_deployment(
            name,
            namespace,
            body=body,
            _request_timeout=self._k8s_timeout,
        )

    def _reconcile_canary(
        self,
        name: str,
        namespace: str,
        spec: dict[str, Any],
        canary: dict[str, Any],
        endpoint: dict[str, Any],
    ) -> dict[str, Any]:
        """Reconcile a classic canary and return observed readiness for routing."""
        canary_image_value = canary.get("image")
        if not isinstance(canary_image_value, str) or not canary_image_value.strip():
            raise ValueError("canary.image must be a non-empty string")
        canary_image = canary_image_value.strip()

        canary_replicas = canary.get("replicas", 1)
        if (
            not isinstance(canary_replicas, int)
            or isinstance(canary_replicas, bool)
            or canary_replicas < 1
        ):
            raise ValueError("canary.replicas must be a positive integer")

        canary_weight = canary.get("weight", 10)
        if (
            not isinstance(canary_weight, int)
            or isinstance(canary_weight, bool)
            or not 1 <= canary_weight <= 99
        ):
            raise ValueError("canary.weight must be an integer between 1 and 99")

        canary_name = f"{name}-canary"
        del endpoint  # Per-endpoint routing metadata is legacy and intentionally ignored.

        canary_spec = dict(spec)
        canary_spec["image"] = canary_image
        canary_spec["replicas"] = canary_replicas
        canary_spec.pop("canary", None)
        # A canary image is explicit and global. Retaining the primary's
        # region_image_uris would silently deploy the old regional image.
        canary_spec.pop("region_image_uris", None)

        deployment = self._get_deployment(canary_name, namespace)
        state = "creating"
        ready_replicas = 0
        if deployment is None:
            logger.info("Creating canary deployment %s with image %s", canary_name, canary_image)
            self._create_deployment(canary_name, namespace, canary_spec)
            self._create_service(canary_name, namespace, canary_spec)
        else:
            self._ensure_service(canary_name, namespace, canary_spec)
            current_image = self._get_deployment_image(deployment)
            current_replicas = deployment.spec.replicas or 1
            ready_replicas = deployment.status.ready_replicas or 0
            if current_image != canary_image:
                self._update_deployment_image(canary_name, namespace, canary_image)
                ready_replicas = 0
                state = "updating"
            elif current_replicas != canary_replicas:
                self._scale_deployment(canary_name, namespace, canary_replicas)
                ready_replicas = min(ready_replicas, canary_replicas)
                state = "updating"
            elif ready_replicas >= canary_replicas:
                state = "running"

        # Canary selection happens behind ``gco-system/inference-proxy``; the
        # shared ``gco-system/gco-gateway`` HTTPRoute is never changed per
        # endpoint. The shared inference proxy consumes the observed canary
        # status returned here.
        return {
            "state": state,
            "image": canary_image,
            "weight": canary_weight,
            "replicas_ready": ready_replicas,
            "replicas_desired": canary_replicas,
        }

    def _cleanup_canary(self, name: str, namespace: str) -> None:
        """Remove only canary objects carrying this reconciliation authority."""
        canary_name = f"{name}-canary"
        deployment = self._get_deployment(canary_name, namespace)
        if deployment is not None:
            self._assert_mutation_authority()
            try:
                self.apps_v1.delete_namespaced_deployment(
                    canary_name,
                    namespace,
                    body=self._delete_options_for(
                        deployment, kind="deployment", resource_name=canary_name
                    ),
                    _request_timeout=self._k8s_timeout,
                )
                logger.info("Deleted canary deployment %s", canary_name)
            except ApiException as error:
                if error.status != 404:
                    logger.error("Failed to delete canary deployment %s: %s", canary_name, error)
        try:
            service = self._authorize_existing_service(canary_name, namespace)
        except ApiException as error:
            if error.status != 404:
                logger.error("Failed to read canary service %s: %s", canary_name, error)
        else:
            self._assert_mutation_authority()
            try:
                self.core_v1.delete_namespaced_service(
                    canary_name,
                    namespace,
                    body=self._delete_options_for(
                        service, kind="service", resource_name=canary_name
                    ),
                    _request_timeout=self._k8s_timeout,
                )
                logger.info("Deleted canary service %s", canary_name)
            except ApiException as error:
                if error.status != 404:
                    logger.error("Failed to delete canary service %s: %s", canary_name, error)

    @staticmethod
    def _endpoint_resource_inventory(name: str) -> EndpointResourceInventory:
        """Return every deterministic endpoint-owned resource name.

        The shared ``mooncake-master`` Service/StatefulSet and any Secret named
        by the user are deliberately excluded.
        """
        return EndpointResourceInventory(
            deployments=(
                name,
                f"{name}-canary",
                f"{name}-prefill",
                f"{name}-decode",
                f"{name}-proxy",
            ),
            services=(
                name,
                f"{name}-canary",
                f"{name}-prefill",
                f"{name}-decode",
                f"{name}-proxy",
            ),
            horizontal_pod_autoscalers=(
                name,
                f"{name}-prefill",
                f"{name}-decode",
                f"keda-hpa-{name}",
                f"keda-hpa-{name}-prefill",
                f"keda-hpa-{name}-decode",
            ),
            scaled_objects=(
                name,
                f"{name}-prefill",
                f"{name}-decode",
            ),
            config_maps=(f"{name}-mooncake", f"{name}-pd-proxy"),
            legacy_ingresses=(name, f"{name}-canary", f"{name}-proxy"),
            legacy_http_routes=(name, f"{name}-canary", f"{name}-proxy"),
            generated_admin_secret=f"{name}-admin",
        )

    @staticmethod
    def _cleanup_error(
        operation: str,
        kind: str,
        resource_name: str,
        error: Exception,
    ) -> str:
        """Build a bounded error containing only an endpoint-owned name."""
        status_value = getattr(error, "status", None)
        status = f" (status {status_value})" if status_value is not None else ""
        return f"{operation} {kind} {resource_name} failed{status}"

    def _delete_and_confirm(
        self,
        *,
        kind: str,
        resource_name: str,
        delete_call: Callable[..., Any],
        read_call: Callable[[], Any],
        patch_metadata: Callable[..., Any] | None,
        pending: list[str],
        errors: list[str],
        observed_resource: Any | None = None,
    ) -> bool:
        """Read-authorize-delete one exact UID/resourceVersion, then re-observe."""
        resource_id = f"{kind}/{resource_name}"
        if observed_resource is not None:
            observed = observed_resource
        else:
            try:
                observed = read_call()
            except ApiException as error:
                if error.status == 404:
                    return False
                errors.append(self._cleanup_error("read", kind, resource_name, error))
                return False
            except Exception as error:
                errors.append(self._cleanup_error("read", kind, resource_name, error))
                return False

        observed = self._authorize_resource(
            observed,
            kind=kind,
            resource_name=resource_name,
            patch_metadata=patch_metadata,
            read_resource=read_call,
            delete_resource=delete_call,
            allow_region_mismatch=bool(
                self._active_authority and self._active_authority.region_removed
            ),
        )
        _metadata, _annotations, observed_uid, _resource_version = self._object_metadata(observed)
        self._assert_mutation_authority()
        try:
            delete_call(
                body=self._delete_options_for(observed, kind=kind, resource_name=resource_name)
            )
        except ApiException as error:
            if error.status != 404:
                errors.append(self._cleanup_error("delete", kind, resource_name, error))
        except Exception as error:
            errors.append(self._cleanup_error("delete", kind, resource_name, error))

        try:
            remaining = read_call()
        except ApiException as error:
            if error.status == 404:
                return True
            errors.append(self._cleanup_error("read", kind, resource_name, error))
            return True
        except Exception as error:
            errors.append(self._cleanup_error("read", kind, resource_name, error))
            return True

        _metadata, _annotations, remaining_uid, _remaining_version = self._object_metadata(
            remaining
        )
        # A replacement with a different UID is never deleted by this stale
        # observation. Its continued presence intentionally blocks cleanup.
        if observed_uid and remaining_uid and remaining_uid != observed_uid:
            pending.append(f"{resource_id}:replacement")
        else:
            pending.append(resource_id)
        return True

    @staticmethod
    def _merge_cleanup_results(*results: ResourceCleanupResult) -> ResourceCleanupResult:
        """Combine independently attempted cleanup groups without losing failures."""
        return ResourceCleanupResult(
            pending=tuple(item for result in results for item in result.pending),
            errors=tuple(item for result in results for item in result.errors),
            resources_found=any(result.resources_found for result in results),
        )

    def _delete_scaled_objects(
        self,
        names: tuple[str, ...],
        namespace: str,
    ) -> ResourceCleanupResult:
        """Delete and verify the requested KEDA ScaledObjects."""
        pending: list[str] = []
        errors: list[str] = []
        resources_found = False
        custom_objects = client.CustomObjectsApi()
        for autoscaler_name in names:
            resources_found |= self._delete_and_confirm(
                kind="scaledobject",
                resource_name=autoscaler_name,
                delete_call=partial(
                    custom_objects.delete_namespaced_custom_object,
                    group=KEDA_API_GROUP,
                    version=KEDA_API_VERSION,
                    namespace=namespace,
                    plural=KEDA_SCALEDOBJECT_PLURAL,
                    name=autoscaler_name,
                    _request_timeout=self._k8s_timeout,
                ),
                read_call=partial(
                    custom_objects.get_namespaced_custom_object,
                    group=KEDA_API_GROUP,
                    version=KEDA_API_VERSION,
                    namespace=namespace,
                    plural=KEDA_SCALEDOBJECT_PLURAL,
                    name=autoscaler_name,
                    _request_timeout=self._k8s_timeout,
                ),
                patch_metadata=partial(
                    custom_objects.patch_namespaced_custom_object,
                    group=KEDA_API_GROUP,
                    version=KEDA_API_VERSION,
                    namespace=namespace,
                    plural=KEDA_SCALEDOBJECT_PLURAL,
                    name=autoscaler_name,
                    _request_timeout=self._k8s_timeout,
                ),
                pending=pending,
                errors=errors,
            )
        return ResourceCleanupResult(
            pending=tuple(pending),
            errors=tuple(errors),
            resources_found=resources_found,
        )

    def _delete_hpas(
        self,
        names: tuple[str, ...],
        namespace: str,
    ) -> ResourceCleanupResult:
        """Delete and verify the requested native or KEDA-generated HPAs."""
        pending: list[str] = []
        errors: list[str] = []
        resources_found = False
        autoscaling_v2 = client.AutoscalingV2Api()
        for autoscaler_name in names:
            resources_found |= self._delete_and_confirm(
                kind="hpa",
                resource_name=autoscaler_name,
                delete_call=partial(
                    autoscaling_v2.delete_namespaced_horizontal_pod_autoscaler,
                    autoscaler_name,
                    namespace,
                    _request_timeout=self._k8s_timeout,
                ),
                read_call=partial(
                    autoscaling_v2.read_namespaced_horizontal_pod_autoscaler,
                    autoscaler_name,
                    namespace,
                    _request_timeout=self._k8s_timeout,
                ),
                patch_metadata=partial(
                    autoscaling_v2.patch_namespaced_horizontal_pod_autoscaler,
                    autoscaler_name,
                    namespace,
                    _request_timeout=self._k8s_timeout,
                ),
                pending=pending,
                errors=errors,
            )
        return ResourceCleanupResult(
            pending=tuple(pending),
            errors=tuple(errors),
            resources_found=resources_found,
        )

    def _delete_autoscalers(
        self,
        scaled_object_names: tuple[str, ...],
        hpa_names: tuple[str, ...],
        namespace: str,
    ) -> ResourceCleanupResult:
        """Delete every autoscaler owner and wait for all of them to disappear."""
        # KEDA must stop first; only then is it safe to remove its generated HPA
        # alongside any native HPA that may remain from a prior configuration.
        scaled_objects = self._delete_scaled_objects(scaled_object_names, namespace)
        hpas = self._delete_hpas(hpa_names, namespace)
        return self._merge_cleanup_results(scaled_objects, hpas)

    @staticmethod
    def _generated_admin_secret_labels(expected_name: str) -> dict[str, str]:
        """Return the schema-valid static provenance used by generated Secrets."""
        return {
            "app": expected_name,
            "project": "gco",
            "gco.io/type": "inference",
        }

    @classmethod
    def _is_monitor_owned_admin_secret(
        cls,
        secret: Any,
        expected_name: str,
        expected_lifecycle_id: str,
    ) -> bool:
        """Return whether a generated Secret is bound to this endpoint lifecycle."""
        metadata = getattr(secret, "metadata", None)
        labels = getattr(metadata, "labels", None)
        annotations = getattr(metadata, "annotations", None)
        return (
            labels == cls._generated_admin_secret_labels(expected_name)
            and isinstance(annotations, dict)
            and annotations.get("gco.io/lifecycle-id") == expected_lifecycle_id
        )

    @classmethod
    def _is_legacy_monitor_admin_secret(cls, secret: Any, expected_name: str) -> bool:
        """Recognize only the exact pre-v7 monitor-generated Secret shape."""
        metadata = getattr(secret, "metadata", None)
        labels = getattr(metadata, "labels", None)
        annotations = getattr(metadata, "annotations", None)
        lifecycle_annotation = (
            annotations.get("gco.io/lifecycle-id") if isinstance(annotations, dict) else None
        )
        secret_type = getattr(secret, "type", None)
        return (
            labels == cls._generated_admin_secret_labels(expected_name)
            and lifecycle_annotation is None
            and secret_type == "Opaque"
            and cls._secret_has_admin_api_key(secret)
        )

    def _adopt_legacy_admin_secret(
        self,
        secret: Any,
        secret_name: str,
        namespace: str,
        lifecycle_id: str,
    ) -> Any:
        """Patch exact legacy provenance and verify the persisted lifecycle annotation."""
        if not self._is_legacy_monitor_admin_secret(secret, secret_name):
            raise AdminApiKeySecretError(
                secret_name,
                "the conventional generated Secret has ambiguous ownership",
            )
        metadata = getattr(secret, "metadata", None)
        resource_version = getattr(metadata, "resource_version", None)
        if not isinstance(resource_version, str) or not resource_version:
            raise AdminApiKeySecretError(
                secret_name,
                "legacy generated Secret has no resource version for safe migration",
            )
        annotations = self._provenance_annotations() or {_LIFECYCLE_ANNOTATION: lifecycle_id}
        self._assert_mutation_authority()
        try:
            self.core_v1.patch_namespaced_secret(
                secret_name,
                namespace,
                body={
                    "metadata": {
                        "resourceVersion": resource_version,
                        "annotations": annotations,
                    }
                },
                _request_timeout=self._k8s_timeout,
            )
        except Exception as error:
            raise AdminApiKeySecretError(
                secret_name,
                "legacy generated Secret changed during lifecycle migration",
            ) from error
        adopted = self.core_v1.read_namespaced_secret(
            secret_name,
            namespace,
            _request_timeout=self._k8s_timeout,
        )
        if not self._is_monitor_owned_admin_secret(
            adopted,
            secret_name,
            lifecycle_id,
        ) or not self._secret_has_admin_api_key(adopted):
            raise AdminApiKeySecretError(
                secret_name,
                "legacy generated Secret migration could not be verified",
            )
        return adopted

    def _require_owned_admin_secret(
        self,
        secret: Any,
        secret_name: str,
        namespace: str,
        lifecycle_id: str,
    ) -> Any:
        """Accept current provenance or safely migrate one exact legacy shape."""
        if self._is_monitor_owned_admin_secret(
            secret,
            secret_name,
            lifecycle_id,
        ) and self._secret_has_admin_api_key(secret):
            return secret
        if self._is_legacy_monitor_admin_secret(secret, secret_name):
            return self._adopt_legacy_admin_secret(
                secret,
                secret_name,
                namespace,
                lifecycle_id,
            )
        raise AdminApiKeySecretError(
            secret_name,
            "the conventional generated Secret exists without matching lifecycle provenance",
        )

    @staticmethod
    def _generated_child_matches(
        item: Any,
        kind: str,
        deployment_names: tuple[str, ...],
        service_names: tuple[str, ...],
        replica_set_names: tuple[str, ...] = (),
    ) -> bool:
        """Match generated children only through exact parent identity."""
        metadata = getattr(item, "metadata", None)
        child_name = getattr(metadata, "name", None)
        labels = getattr(metadata, "labels", None)
        labels = labels if isinstance(labels, dict) else {}
        owner_references = getattr(metadata, "owner_references", None)
        owners = owner_references if isinstance(owner_references, (list, tuple)) else ()

        if kind in {"replicaset", "pod"}:
            app_name = labels.get("app")
            if app_name not in deployment_names:
                return False
            if labels.get("project") != "gco" or labels.get("gco.io/type") != "inference":
                return False
            if not owners:
                return True
            for owner in owners:
                owner_kind = getattr(owner, "kind", None)
                owner_name = getattr(owner, "name", None)
                if owner_kind == "Deployment" and owner_name in deployment_names:
                    return True
                if kind == "pod" and owner_kind == "ReplicaSet" and owner_name in replica_set_names:
                    return True
            return False

        if kind == "endpoints":
            return isinstance(child_name, str) and child_name in service_names
        if kind == "endpointslice":
            service_name = labels.get("kubernetes.io/service-name")
            return isinstance(service_name, str) and service_name in service_names
        return False

    def _observe_generated_children(
        self,
        name: str,
        namespace: str,
        inventory: EndpointResourceInventory,
        pending: list[str],
        errors: list[str],
    ) -> bool:
        """Inventory exact endpoint children once per kind and dependency order."""
        resources_found = False
        matched_replica_sets: list[str] = []
        list_calls: tuple[tuple[str, Callable[[], Any]], ...] = (
            (
                "replicaset",
                partial(
                    self.apps_v1.list_namespaced_replica_set,
                    namespace,
                    _request_timeout=self._k8s_timeout,
                ),
            ),
            (
                "pod",
                partial(
                    self.core_v1.list_namespaced_pod,
                    namespace,
                    _request_timeout=self._k8s_timeout,
                ),
            ),
            (
                "endpoints",
                partial(
                    self.core_v1.list_namespaced_endpoints,
                    namespace,
                    _request_timeout=self._k8s_timeout,
                ),
            ),
            (
                "endpointslice",
                partial(
                    self.discovery_v1.list_namespaced_endpoint_slice,
                    namespace,
                    _request_timeout=self._k8s_timeout,
                ),
            ),
        )
        for kind, list_call in list_calls:
            try:
                response = list_call()
            except ApiException as error:
                errors.append(self._cleanup_error("list", kind, name, error))
                continue
            except Exception as error:
                errors.append(self._cleanup_error("list", kind, name, error))
                continue
            items = getattr(response, "items", None)
            if not isinstance(items, (list, tuple)):
                items = ()
            for item in items:
                if not self._generated_child_matches(
                    item,
                    kind,
                    inventory.deployments,
                    inventory.services,
                    tuple(matched_replica_sets),
                ):
                    continue
                metadata = getattr(item, "metadata", None)
                child_name = getattr(metadata, "name", "unknown")
                if kind == "replicaset" and isinstance(child_name, str):
                    matched_replica_sets.append(child_name)
                pending.append(f"{kind}/{child_name}")
                resources_found = True
        return resources_found

    def _delete_resources(
        self,
        name: str,
        namespace: str,
        spec: dict[str, Any] | None = None,
        *,
        expected_lifecycle_id: str | None = None,
    ) -> ResourceCleanupResult:
        """Delete parents and prove all top-level/generated children absent."""
        inventory = self._endpoint_resource_inventory(name)
        autoscaler_cleanup = self._delete_autoscalers(
            inventory.scaled_objects,
            inventory.horizontal_pod_autoscalers,
            namespace,
        )
        pending = list(autoscaler_cleanup.pending)
        errors = list(autoscaler_cleanup.errors)
        resources_found = autoscaler_cleanup.resources_found

        for deployment_name in inventory.deployments:
            resources_found |= self._delete_and_confirm(
                kind="deployment",
                resource_name=deployment_name,
                delete_call=partial(
                    self.apps_v1.delete_namespaced_deployment,
                    deployment_name,
                    namespace,
                    _request_timeout=self._k8s_timeout,
                ),
                read_call=partial(
                    self.apps_v1.read_namespaced_deployment,
                    deployment_name,
                    namespace,
                    _request_timeout=self._k8s_timeout,
                ),
                patch_metadata=partial(
                    self.apps_v1.patch_namespaced_deployment,
                    deployment_name,
                    namespace,
                    _request_timeout=self._k8s_timeout,
                ),
                pending=pending,
                errors=errors,
            )

        for service_name in inventory.services:
            resources_found |= self._delete_and_confirm(
                kind="service",
                resource_name=service_name,
                delete_call=partial(
                    self.core_v1.delete_namespaced_service,
                    service_name,
                    namespace,
                    _request_timeout=self._k8s_timeout,
                ),
                read_call=partial(
                    self.core_v1.read_namespaced_service,
                    service_name,
                    namespace,
                    _request_timeout=self._k8s_timeout,
                ),
                patch_metadata=partial(
                    self.core_v1.patch_namespaced_service,
                    service_name,
                    namespace,
                    _request_timeout=self._k8s_timeout,
                ),
                pending=pending,
                errors=errors,
            )

        for config_map_name in inventory.config_maps:
            resources_found |= self._delete_and_confirm(
                kind="configmap",
                resource_name=config_map_name,
                delete_call=partial(
                    self.core_v1.delete_namespaced_config_map,
                    config_map_name,
                    namespace,
                    _request_timeout=self._k8s_timeout,
                ),
                read_call=partial(
                    self.core_v1.read_namespaced_config_map,
                    config_map_name,
                    namespace,
                    _request_timeout=self._k8s_timeout,
                ),
                patch_metadata=partial(
                    self.core_v1.patch_namespaced_config_map,
                    config_map_name,
                    namespace,
                    _request_timeout=self._k8s_timeout,
                ),
                pending=pending,
                errors=errors,
            )

        for ingress_name in inventory.legacy_ingresses:
            resources_found |= self._delete_and_confirm(
                kind="ingress",
                resource_name=ingress_name,
                delete_call=partial(
                    self.networking_v1.delete_namespaced_ingress,
                    ingress_name,
                    namespace,
                    _request_timeout=self._k8s_timeout,
                ),
                read_call=partial(
                    self.networking_v1.read_namespaced_ingress,
                    ingress_name,
                    namespace,
                    _request_timeout=self._k8s_timeout,
                ),
                patch_metadata=partial(
                    self.networking_v1.patch_namespaced_ingress,
                    ingress_name,
                    namespace,
                    _request_timeout=self._k8s_timeout,
                ),
                pending=pending,
                errors=errors,
            )

        custom_objects = client.CustomObjectsApi()
        for route_name in inventory.legacy_http_routes:
            resources_found |= self._delete_and_confirm(
                kind="httproute",
                resource_name=route_name,
                delete_call=partial(
                    custom_objects.delete_namespaced_custom_object,
                    group="gateway.networking.k8s.io",
                    version="v1",
                    namespace=namespace,
                    plural="httproutes",
                    name=route_name,
                    _request_timeout=self._k8s_timeout,
                ),
                read_call=partial(
                    custom_objects.get_namespaced_custom_object,
                    group="gateway.networking.k8s.io",
                    version="v1",
                    namespace=namespace,
                    plural="httproutes",
                    name=route_name,
                    _request_timeout=self._k8s_timeout,
                ),
                patch_metadata=partial(
                    custom_objects.patch_namespaced_custom_object,
                    group="gateway.networking.k8s.io",
                    version="v1",
                    namespace=namespace,
                    plural="httproutes",
                    name=route_name,
                    _request_timeout=self._k8s_timeout,
                ),
                pending=pending,
                errors=errors,
            )

        resources_found |= self._observe_generated_children(
            name,
            namespace,
            inventory,
            pending,
            errors,
        )

        # A user-named Secret is external and survives. The conventional name
        # is auto-managed only when both labels and immutable provenance match;
        # an ambiguous same-name Secret blocks terminal cleanup.
        mooncake = spec.get("mooncake") if isinstance(spec, dict) else None
        proxy = mooncake.get("proxy") if isinstance(mooncake, dict) else None
        named_secret = proxy.get("admin_api_key_secret") if isinstance(proxy, dict) else None
        generated_secret = inventory.generated_admin_secret
        if named_secret != generated_secret:
            try:
                secret = self.core_v1.read_namespaced_secret(
                    generated_secret,
                    namespace,
                    _request_timeout=self._k8s_timeout,
                )
            except ApiException as error:
                if error.status != 404:
                    errors.append(self._cleanup_error("read", "secret", generated_secret, error))
            except Exception as error:
                errors.append(self._cleanup_error("read", "secret", generated_secret, error))
            else:
                resources_found = True
                if isinstance(expected_lifecycle_id, str) and expected_lifecycle_id:
                    try:
                        owned_secret = self._require_owned_admin_secret(
                            secret,
                            generated_secret,
                            namespace,
                            expected_lifecycle_id,
                        )
                    except AdminApiKeySecretError:
                        errors.append(
                            f"ambiguous secret {generated_secret} is not owned by lifecycle "
                            f"{expected_lifecycle_id}"
                        )
                    else:
                        resources_found |= self._delete_and_confirm(
                            kind="secret",
                            resource_name=generated_secret,
                            delete_call=partial(
                                self.core_v1.delete_namespaced_secret,
                                generated_secret,
                                namespace,
                                _request_timeout=self._k8s_timeout,
                            ),
                            read_call=partial(
                                self.core_v1.read_namespaced_secret,
                                generated_secret,
                                namespace,
                                _request_timeout=self._k8s_timeout,
                            ),
                            patch_metadata=partial(
                                self.core_v1.patch_namespaced_secret,
                                generated_secret,
                                namespace,
                                _request_timeout=self._k8s_timeout,
                            ),
                            pending=pending,
                            errors=errors,
                            observed_resource=owned_secret,
                        )
                else:
                    errors.append(
                        f"ambiguous secret {generated_secret} is not owned by lifecycle unknown"
                    )

        return ResourceCleanupResult(
            pending=tuple(sorted(set(pending))),
            errors=tuple(errors),
            resources_found=resources_found,
        )

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
        body: dict[str, Any] = {
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
                "annotations": self._provenance_annotations() or {},
            },
            "spec": {
                "scaleTargetRef": {"name": target_name},
                "minReplicaCount": min_replicas,
                "maxReplicaCount": max_replicas,
                "triggers": self._build_keda_triggers(metrics_config, target_name, namespace),
            },
        }

        custom = client.CustomObjectsApi()
        self._assert_mutation_authority()
        try:
            custom.create_namespaced_custom_object(
                group=KEDA_API_GROUP,
                version=KEDA_API_VERSION,
                namespace=namespace,
                plural=KEDA_SCALEDOBJECT_PLURAL,
                body=body,
                _request_timeout=self._k8s_timeout,
            )
            self._confirm_created_resource(
                kind="scaledobject",
                resource_name=name,
                read_resource=partial(
                    custom.get_namespaced_custom_object,
                    group=KEDA_API_GROUP,
                    version=KEDA_API_VERSION,
                    namespace=namespace,
                    plural=KEDA_SCALEDOBJECT_PLURAL,
                    name=name,
                    _request_timeout=self._k8s_timeout,
                ),
                delete_resource=partial(
                    custom.delete_namespaced_custom_object,
                    group=KEDA_API_GROUP,
                    version=KEDA_API_VERSION,
                    namespace=namespace,
                    plural=KEDA_SCALEDOBJECT_PLURAL,
                    name=name,
                    _request_timeout=self._k8s_timeout,
                ),
            )
            logger.info(
                "Created KEDA ScaledObject %s targeting %s (min=%d, max=%d)",
                name,
                target_name,
                min_replicas,
                max_replicas,
            )
        except ApiException as error:
            if error.status != 409:
                raise
            existing = custom.get_namespaced_custom_object(
                group=KEDA_API_GROUP,
                version=KEDA_API_VERSION,
                namespace=namespace,
                plural=KEDA_SCALEDOBJECT_PLURAL,
                name=name,
                _request_timeout=self._k8s_timeout,
            )
            existing = self._authorize_resource(
                existing,
                kind="scaledobject",
                resource_name=name,
                patch_metadata=partial(
                    custom.patch_namespaced_custom_object,
                    group=KEDA_API_GROUP,
                    version=KEDA_API_VERSION,
                    namespace=namespace,
                    plural=KEDA_SCALEDOBJECT_PLURAL,
                    name=name,
                    _request_timeout=self._k8s_timeout,
                ),
                read_resource=lambda: custom.get_namespaced_custom_object(
                    group=KEDA_API_GROUP,
                    version=KEDA_API_VERSION,
                    namespace=namespace,
                    plural=KEDA_SCALEDOBJECT_PLURAL,
                    name=name,
                    _request_timeout=self._k8s_timeout,
                ),
                delete_resource=partial(
                    custom.delete_namespaced_custom_object,
                    group=KEDA_API_GROUP,
                    version=KEDA_API_VERSION,
                    namespace=namespace,
                    plural=KEDA_SCALEDOBJECT_PLURAL,
                    name=name,
                    _request_timeout=self._k8s_timeout,
                ),
            )
            _metadata, _annotations, _uid, resource_version = self._object_metadata(existing)
            body["metadata"]["resourceVersion"] = resource_version
            self._assert_mutation_authority()
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
                annotations=self._provenance_annotations(),
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
        self._assert_mutation_authority()
        try:
            autoscaling_v2.create_namespaced_horizontal_pod_autoscaler(namespace, hpa)
            self._confirm_created_resource(
                kind="hpa",
                resource_name=hpa_name,
                read_resource=partial(
                    autoscaling_v2.read_namespaced_horizontal_pod_autoscaler,
                    hpa_name,
                    namespace,
                    _request_timeout=self._k8s_timeout,
                ),
                delete_resource=partial(
                    autoscaling_v2.delete_namespaced_horizontal_pod_autoscaler,
                    hpa_name,
                    namespace,
                    _request_timeout=self._k8s_timeout,
                ),
            )
            logger.info(
                "Created HPA %s targeting %s (min=%d, max=%d)",
                hpa_name,
                target_name,
                min_replicas,
                max_replicas,
            )
        except ApiException as error:
            if error.status != 409:
                raise
            existing = autoscaling_v2.read_namespaced_horizontal_pod_autoscaler(
                hpa_name, namespace, _request_timeout=self._k8s_timeout
            )
            existing = self._authorize_resource(
                existing,
                kind="hpa",
                resource_name=hpa_name,
                patch_metadata=partial(
                    autoscaling_v2.patch_namespaced_horizontal_pod_autoscaler,
                    hpa_name,
                    namespace,
                    _request_timeout=self._k8s_timeout,
                ),
                read_resource=lambda: autoscaling_v2.read_namespaced_horizontal_pod_autoscaler(
                    hpa_name, namespace, _request_timeout=self._k8s_timeout
                ),
                delete_resource=partial(
                    autoscaling_v2.delete_namespaced_horizontal_pod_autoscaler,
                    hpa_name,
                    namespace,
                    _request_timeout=self._k8s_timeout,
                ),
            )
            _metadata, _annotations, _uid, resource_version = self._object_metadata(existing)
            hpa.metadata.resource_version = resource_version
            self._assert_mutation_authority()
            autoscaling_v2.patch_namespaced_horizontal_pod_autoscaler(
                hpa_name,
                namespace,
                hpa,
                _request_timeout=self._k8s_timeout,
            )
            logger.info("Updated HPA %s", hpa_name)

    def _verify_hpa_owner(
        self,
        hpa_name: str,
        namespace: str,
        target_name: str,
    ) -> ResourceCleanupResult:
        """Verify that the exact native/KEDA HPA owns the expected Deployment."""
        autoscaling_v2 = client.AutoscalingV2Api()
        try:
            hpa = autoscaling_v2.read_namespaced_horizontal_pod_autoscaler(
                hpa_name,
                namespace,
            )
        except ApiException as error:
            if error.status == 404:
                return ResourceCleanupResult(pending=(f"hpa/{hpa_name}",))
            return ResourceCleanupResult(
                errors=(self._cleanup_error("read", "hpa", hpa_name, error),)
            )
        except Exception as error:
            return ResourceCleanupResult(
                errors=(self._cleanup_error("read", "hpa", hpa_name, error),)
            )
        try:
            hpa = self._authorize_resource(
                hpa,
                kind="hpa",
                resource_name=hpa_name,
                patch_metadata=partial(
                    autoscaling_v2.patch_namespaced_horizontal_pod_autoscaler,
                    hpa_name,
                    namespace,
                    _request_timeout=self._k8s_timeout,
                ),
                read_resource=lambda: autoscaling_v2.read_namespaced_horizontal_pod_autoscaler(
                    hpa_name, namespace, _request_timeout=self._k8s_timeout
                ),
                delete_resource=partial(
                    autoscaling_v2.delete_namespaced_horizontal_pod_autoscaler,
                    hpa_name,
                    namespace,
                    _request_timeout=self._k8s_timeout,
                ),
            )
        except ReconcileFencedError:
            raise
        hpa_spec = getattr(hpa, "spec", None)
        target_ref = getattr(hpa_spec, "scale_target_ref", None)
        observed_api_version = getattr(target_ref, "api_version", None)
        observed_kind = getattr(target_ref, "kind", None)
        observed_target = getattr(target_ref, "name", None)
        if (
            observed_api_version != "apps/v1"
            or observed_kind != "Deployment"
            or observed_target != target_name
        ):
            return ResourceCleanupResult(
                pending=(f"hpa/{hpa_name}",),
                resources_found=True,
            )
        return ResourceCleanupResult(resources_found=True)

    def _reconcile_classic_autoscaler(
        self,
        name: str,
        namespace: str,
        spec: dict[str, Any],
        *,
        apply_desired: bool = True,
    ) -> ResourceCleanupResult:
        """Converge a classic endpoint to exactly one autoscaler owner.

        A stopped target is first restored to a positive minimum, then the
        desired native/KEDA owner is applied and read back before reconciliation
        may proceed. Obsolete owners are always confirmed absent first.
        """
        autoscaling = spec.get("autoscaling", {})
        metrics_config = autoscaling.get("metrics", [{"type": "cpu", "target": 70}])
        keda = self._metrics_require_keda(metrics_config)
        if keda:
            cleanup = self._delete_hpas((name,), namespace)
        else:
            cleanup = self._merge_cleanup_results(
                self._delete_scaled_objects((name,), namespace),
                self._delete_hpas((f"keda-hpa-{name}",), namespace),
            )

        if cleanup.complete and apply_desired:
            target = self._get_deployment(name, namespace)
            current = getattr(getattr(target, "spec", None), "replicas", None)
            if target is not None and current == 0:
                minimum = max(1, int(autoscaling.get("min_replicas", 1)))
                self._scale_deployment(name, namespace, minimum)
            self._create_or_update_hpa(name, namespace, spec)
            desired_hpa = f"keda-hpa-{name}" if keda else name
            return self._merge_cleanup_results(
                cleanup,
                self._verify_hpa_owner(desired_hpa, namespace, name),
            )
        return cleanup

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

    @staticmethod
    def _role_autoscaling_config(
        spec: dict[str, Any],
        role: str,
    ) -> dict[str, Any] | None:
        """Return an enabled role config, or ``None`` for static ownership."""
        mooncake = spec.get("mooncake")
        if not isinstance(mooncake, dict):
            return None
        autoscaling = mooncake.get("autoscaling")
        if not isinstance(autoscaling, dict) or autoscaling.get("enabled") is not True:
            return None
        role_config = autoscaling.get(role)
        return role_config if isinstance(role_config, dict) else None

    def _reconcile_role_autoscaler(
        self,
        name: str,
        namespace: str,
        spec: dict[str, Any],
        role: str,
        *,
        apply_desired: bool = True,
    ) -> ResourceCleanupResult:
        """Converge one Mooncake role to exactly one or zero replica owners."""
        target_name = f"{name}-{role}"
        role_config = self._role_autoscaling_config(spec, role)
        if role_config is None:
            return self._delete_autoscalers(
                (target_name,),
                (target_name, f"keda-hpa-{target_name}"),
                namespace,
            )

        metrics_config = role_config.get("metrics", [{"type": "cpu", "target": 70}])
        if self._metrics_require_keda(metrics_config):
            cleanup = self._delete_hpas((target_name,), namespace)
        else:
            cleanup = self._merge_cleanup_results(
                self._delete_scaled_objects((target_name,), namespace),
                self._delete_hpas((f"keda-hpa-{target_name}",), namespace),
            )
        if cleanup.complete and apply_desired:
            self._create_role_hpa(name, namespace, spec, role)
        return cleanup

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
        role_cfg = self._role_autoscaling_config(spec, role)
        if role_cfg is None:
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

    # Expose Prometheus metrics on a dedicated port for the in-cluster
    # observability scrape. A scrape-time collector reflects the monitor's live
    # counters (reconcile_count, errors_count, running), so no push from the
    # reconcile loop is needed.
    from gco.services.service_metrics import start_metrics_server

    metrics_port = int(os.getenv("METRICS_PORT", "9090"))
    start_metrics_server(metrics_port, "inference-monitor", monitor.get_metrics)

    # Kubernetes stops pods with SIGTERM. This process is PID 1 in its
    # container, and PID 1 receives no kernel-default signal handling — so
    # without an explicit handler SIGTERM was silently ignored, every pod
    # rotation burned the full terminationGracePeriodSeconds, and the kubelet
    # SIGKILLed the monitor mid-reconcile (exit 137). The handler flips the
    # same stop flag the reconcile loop already honors, so shutdown waits for
    # the in-flight cycle and exits 0 within one reconcile interval.
    shutdown_requested = asyncio.Event()
    loop = asyncio.get_running_loop()

    def _handle_sigterm() -> None:
        logger.info("SIGTERM received; stopping inference monitor after current cycle")
        shutdown_requested.set()
        monitor.stop()

    try:
        loop.add_signal_handler(signal.SIGTERM, _handle_sigterm)
    except NotImplementedError:
        # Non-POSIX event loops (Windows dev environments running the unit
        # suite) don't support loop signal handlers; in the Linux container
        # this always succeeds.
        logger.debug("Event loop does not support signal handlers; skipping SIGTERM hook")

    try:
        while not shutdown_requested.is_set():
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
        logger.info("Inference monitor exited cleanly")
    finally:
        with contextlib.suppress(NotImplementedError, ValueError):
            loop.remove_signal_handler(signal.SIGTERM)


if __name__ == "__main__":
    asyncio.run(main())
