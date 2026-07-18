"""
Inference endpoint management for GCO CLI.

Provides functionality to deploy, manage, and monitor inference endpoints
across multi-region EKS clusters via the DynamoDB-backed reconciliation
pattern (inference_monitor).
"""

from __future__ import annotations

import logging
from copy import deepcopy
from typing import TYPE_CHECKING, Any, Literal, TypedDict, TypeGuard

from .aws_client import get_aws_client
from .config import GCOConfig, get_config

# <pyflowchart-code-diagram> BEGIN - auto-inserted, do not edit
# Generated at (UTC): 2026-07-18T01:03:40Z
# Flowchart(s) generated from this file:
#   * ``InferenceManager.deploy`` -> ``diagrams/code_diagrams/cli/inference.InferenceManager_deploy.html``
#     (PNG: ``diagrams/code_diagrams/cli/inference.InferenceManager_deploy.png``)
#   * ``InferenceManager.canary_deploy`` -> ``diagrams/code_diagrams/cli/inference.InferenceManager_canary_deploy.html``
#     (PNG: ``diagrams/code_diagrams/cli/inference.InferenceManager_canary_deploy.png``)
# Regenerate with ``python diagrams/code_diagrams/generate.py``.
# <pyflowchart-code-diagram> END


if TYPE_CHECKING:
    from gco.services.inference_store import InferenceEndpointStore

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Mooncake topology — optional endpoint-spec extension
# ---------------------------------------------------------------------------
#
# An endpoint spec may carry an optional ``mooncake`` block describing
# disaggregated prefill/decode (PD) serving and/or a shared KV-cache store.
# The block is entirely additive: when it is absent the endpoint reconciles
# exactly as it does today — one Deployment and one internal ClusterIP Service
# behind the shared authenticated inference route.
#
# The definitions below describe the shape of that block (the dict written to
# DynamoDB and read back by the per-region monitor) and the constant
# vocabularies its enumerated fields draw from. Byte-size fields are authored
# as base-10 integer decimal strings (see :func:`author_byte_size`) so they
# round-trip through DynamoDB without being coerced to ``Decimal`` via a float
# literal.

#: Serving modes a ``mooncake`` block may declare.
#: ``disaggregated`` splits prefill and decode; ``store`` runs a single
#: KV-store instance; ``both`` composes the two.
MOONCAKE_MODES: frozenset[str] = frozenset({"disaggregated", "store", "both"})

#: KV transfer / store intents. ``rdma`` is the default high-performance
#: intent: GCO schedules the pod on EFA and renders vLLM's point-to-point
#: ``mooncake_protocol`` as ``efa``. ``tcp`` is the non-EFA fallback.
MOONCAKE_TRANSFER_PROTOCOLS: frozenset[str] = frozenset({"rdma", "tcp"})

#: KV-store offload tiers for spilling cache beyond GPU memory.
MOONCAKE_OFFLOAD_TIERS: frozenset[str] = frozenset({"cpu", "disk", "none"})

#: PD proxy request-scheduling strategies supported today.
MOONCAKE_PROXY_SCHEDULING: frozenset[str] = frozenset({"round_robin"})

#: Inclusive bounds for per-role replica counts in an XpYd topology.
MOONCAKE_TOPOLOGY_MIN: int = 1
MOONCAKE_TOPOLOGY_MAX: int = 1000

#: Inclusive bounds for byte-size fields. The ceiling is the signed 64-bit
#: maximum; authoring sizes as decimal strings in ``[MIN, MAX]`` keeps them out
#: of float/Decimal coercion when they round-trip through DynamoDB.
MOONCAKE_BYTE_SIZE_MIN: int = 0
MOONCAKE_BYTE_SIZE_MAX: int = 9223372036854775807

#: Transfer-engine defaults mirroring Mooncake's reference configuration.
MOONCAKE_DEFAULT_BOOTSTRAP_BASE_PORT: int = 8998
MOONCAKE_DEFAULT_NUM_WORKERS: int = 10
MOONCAKE_DEFAULT_ABORT_REQUEST_TIMEOUT: int = 480


class MooncakeTopology(TypedDict):
    """An XpYd topology: ``prefill`` (X) and ``decode`` (Y) instance counts."""

    prefill: int
    decode: int


class MooncakeStoreConfig(TypedDict, total=False):
    """KV-cache store pool configuration.

    ``global_segment_size`` and ``local_buffer_size`` are byte counts authored
    as base-10 integer decimal strings. ``cold_tier_enabled`` opts this
    endpoint into the asynchronous, per-region object-store cold tier; the
    cold-tier bucket is resolved by the monitor from regional configuration and
    is never a user-typed URI.
    """

    enabled: bool
    metadata_server: str
    master_server_address: str
    protocol: Literal["rdma", "tcp"]
    device_name: str
    global_segment_size: str
    local_buffer_size: str
    offload: Literal["cpu", "disk", "none"]
    cold_tier_enabled: bool


class MooncakeTransferConfig(TypedDict, total=False):
    """RDMA/TCP transfer-engine configuration for KV cache movement."""

    protocol: Literal["rdma", "tcp"]
    device_name: str
    num_workers: int
    bootstrap_base_port: int
    abort_request_timeout: int


class MooncakeProxyConfig(TypedDict, total=False):
    """PD proxy configuration. ``admin_api_key_secret`` names the Kubernetes
    Secret holding the proxy admin key; the key value is never carried on the
    endpoint spec."""

    image: str
    scheduling: Literal["round_robin"]
    admin_api_key_secret: str


class MooncakeRoleAutoscaling(TypedDict, total=False):
    """Per-role autoscaling bounds and metrics for one of prefill/decode."""

    min_replicas: int
    max_replicas: int
    metrics: list[dict[str, Any]]


class MooncakeAutoscalingConfig(TypedDict, total=False):
    """Optional per-role pod autoscaling. When absent the topology is static."""

    enabled: bool
    prefill: MooncakeRoleAutoscaling
    decode: MooncakeRoleAutoscaling


class MooncakeSpec(TypedDict, total=False):
    """The optional ``mooncake`` block carried on an endpoint spec dict."""

    mode: Literal["disaggregated", "store", "both"]
    topology: MooncakeTopology
    store: MooncakeStoreConfig
    transfer: MooncakeTransferConfig
    proxy: MooncakeProxyConfig
    autoscaling: MooncakeAutoscalingConfig


def author_byte_size(value: int | str) -> str:
    """Render a byte-size value as a canonical base-10 integer decimal string.

    Mooncake store/transfer sizes (segment size, local buffer) are carried on
    the endpoint spec as digit-only strings so they survive the DynamoDB
    round-trip without being coerced to ``Decimal`` through a float literal.

    Accepts a non-negative ``int`` or a string of base-10 ASCII digits and
    returns the same whole number as ``str``. The value must fall in
    ``[MOONCAKE_BYTE_SIZE_MIN, MOONCAKE_BYTE_SIZE_MAX]``. Signs, decimal
    points, exponents, floats, booleans, and any non-digit text are not
    accepted.

    Raises:
        ValueError: when ``value`` cannot be authored as an in-range base-10
            integer.
    """
    # ``bool`` is a subclass of ``int``; reject it explicitly so ``True``/``False``
    # never masquerade as 1/0 byte sizes.
    if isinstance(value, bool):
        raise ValueError(f"byte-size value must be an integer, got bool: {value!r}")

    if isinstance(value, int):
        size = value
    elif isinstance(value, str):
        text = value.strip()
        if not text or any(ch not in "0123456789" for ch in text):
            raise ValueError(
                "byte-size value must be a base-10 integer string "
                f"(ASCII digits only, no sign, point, or exponent), got {value!r}"
            )
        size = int(text)
    else:
        raise ValueError(
            f"byte-size value must be an int or a base-10 digit string, got {type(value).__name__}"
        )

    if not MOONCAKE_BYTE_SIZE_MIN <= size <= MOONCAKE_BYTE_SIZE_MAX:
        raise ValueError(
            "byte-size value out of range "
            f"[{MOONCAKE_BYTE_SIZE_MIN}, {MOONCAKE_BYTE_SIZE_MAX}]: {size}"
        )

    return str(size)


#: Byte-size fields a ``mooncake`` store block may carry. Each is authored as a
#: base-10 integer decimal string via :func:`author_byte_size`.
_MOONCAKE_STORE_BYTE_SIZE_FIELDS: tuple[str, ...] = (
    "global_segment_size",
    "local_buffer_size",
)

#: Modes that run a split prefill/decode topology and therefore require a
#: valid ``topology`` and may carry per-role autoscaling.
_MOONCAKE_DISAGGREGATED_MODES: frozenset[str] = frozenset({"disaggregated", "both"})


def _is_plain_int(value: Any) -> TypeGuard[int]:
    """True when ``value`` is an ``int`` and not a ``bool``.

    ``bool`` is a subclass of ``int``; counts and replica bounds must be real
    integers, so ``True``/``False`` are not accepted as 1/0.
    """
    return isinstance(value, int) and not isinstance(value, bool)


def _validate_role_autoscaling_bounds(role: str, role_block: dict[str, Any]) -> None:
    """Validate one role's ``min_replicas``/``max_replicas`` bounds.

    Raises :class:`ValueError` naming the violated bound. ``min_replicas`` must
    be an integer ``>= 1`` and ``max_replicas`` an integer no smaller than the
    effective minimum (which defaults to 1 when ``min_replicas`` is absent).
    """
    min_replicas = role_block.get("min_replicas")
    max_replicas = role_block.get("max_replicas")

    if min_replicas is not None:
        if not _is_plain_int(min_replicas):
            raise ValueError(
                f"mooncake.autoscaling.{role}.min_replicas must be an integer, got {min_replicas!r}"
            )
        if min_replicas < 1:
            raise ValueError(
                f"mooncake.autoscaling.{role}.min_replicas must be >= 1, got {min_replicas}"
            )

    if max_replicas is not None:
        if not _is_plain_int(max_replicas):
            raise ValueError(
                f"mooncake.autoscaling.{role}.max_replicas must be an integer, got {max_replicas!r}"
            )
        effective_min = min_replicas if _is_plain_int(min_replicas) else 1
        if max_replicas < effective_min:
            raise ValueError(
                f"mooncake.autoscaling.{role}.max_replicas ({max_replicas}) "
                f"must be >= min_replicas ({effective_min})"
            )


def validate_mooncake_spec(mooncake: dict[str, Any]) -> None:
    """Validate a ``mooncake`` endpoint-spec block, failing fast.

    Raises :class:`ValueError` on the first rejected field, naming the
    offending field so the caller can correct it. The check is pure — it reads
    nothing and writes nothing — so a caller that validates before persisting
    leaves any previously stored spec untouched when a block is rejected.

    The rules enforced here are:

    * ``mode`` must be one of the supported serving modes
      (:data:`MOONCAKE_MODES`).
    * ``transfer`` must be a mapping when present; ``protocol`` must be one of
      :data:`MOONCAKE_TRANSFER_PROTOCOLS` and ``device_name`` must be a string
      (the empty string requests automatic interface detection).
    * Store byte-size fields must author as in-range base-10 integers.
    * ``disaggregated``/``both`` modes require integer ``topology.prefill`` and
      ``topology.decode`` in
      ``[MOONCAKE_TOPOLOGY_MIN, MOONCAKE_TOPOLOGY_MAX]``.
    * ``store.cold_tier_enabled`` may be true only while ``store.enabled`` is
      true (the cold tier extends the hot store).
    * Autoscaling may be enabled only for ``disaggregated``/``both`` modes, and
      each present role's ``min_replicas``/``max_replicas`` must satisfy
      ``min_replicas >= 1`` and ``max_replicas >= min_replicas``.
    """
    if not isinstance(mooncake, dict):
        raise ValueError("mooncake block must be a mapping")

    mode = mooncake.get("mode")
    if mode not in MOONCAKE_MODES:
        allowed = ", ".join(sorted(MOONCAKE_MODES))
        raise ValueError(f"mooncake.mode must be one of {{{allowed}}}, got {mode!r}")

    store = mooncake.get("store")
    if store is not None and not isinstance(store, dict):
        raise ValueError("mooncake.store must be a mapping")

    transfer = mooncake.get("transfer")
    if transfer is not None and not isinstance(transfer, dict):
        raise ValueError("mooncake.transfer must be a mapping")
    if isinstance(transfer, dict):
        protocol = transfer.get("protocol", "rdma")
        if protocol not in MOONCAKE_TRANSFER_PROTOCOLS:
            allowed = ", ".join(sorted(MOONCAKE_TRANSFER_PROTOCOLS))
            raise ValueError(
                f"mooncake.transfer.protocol must be one of {{{allowed}}}, got {protocol!r}"
            )
        device_name = transfer.get("device_name", "")
        if not isinstance(device_name, str):
            raise ValueError(f"mooncake.transfer.device_name must be a string, got {device_name!r}")

    # Byte-size fields must author cleanly; surface the offending field name.
    if isinstance(store, dict):
        for field in _MOONCAKE_STORE_BYTE_SIZE_FIELDS:
            if field in store:
                try:
                    author_byte_size(store[field])
                except ValueError as exc:
                    raise ValueError(f"mooncake.store.{field}: {exc}") from exc

    # Split topologies need integer prefill/decode counts in range.
    if mode in _MOONCAKE_DISAGGREGATED_MODES:
        topology = mooncake.get("topology")
        if not isinstance(topology, dict):
            raise ValueError(
                f"mooncake.topology is required for mode {mode!r} with integer "
                "'prefill' and 'decode' counts"
            )
        for field in ("prefill", "decode"):
            count = topology.get(field)
            if not _is_plain_int(count):
                raise ValueError(
                    f"mooncake.topology.{field} must be an integer in "
                    f"[{MOONCAKE_TOPOLOGY_MIN}, {MOONCAKE_TOPOLOGY_MAX}], "
                    f"got {count!r}"
                )
            if not MOONCAKE_TOPOLOGY_MIN <= count <= MOONCAKE_TOPOLOGY_MAX:
                raise ValueError(
                    f"mooncake.topology.{field} out of range "
                    f"[{MOONCAKE_TOPOLOGY_MIN}, {MOONCAKE_TOPOLOGY_MAX}]: {count}"
                )

    # The cold tier extends the hot store; it cannot be enabled on its own.
    if (
        isinstance(store, dict)
        and store.get("cold_tier_enabled") is True
        and store.get("enabled") is not True
    ):
        raise ValueError(
            "mooncake.store.cold_tier_enabled requires mooncake.store.enabled to be true"
        )

    autoscaling = mooncake.get("autoscaling")
    if autoscaling is not None:
        if not isinstance(autoscaling, dict):
            raise ValueError("mooncake.autoscaling must be a mapping")
        if autoscaling.get("enabled") is True and mode not in _MOONCAKE_DISAGGREGATED_MODES:
            raise ValueError(
                "mooncake.autoscaling.enabled requires a 'disaggregated' or "
                f"'both' mode, got {mode!r}"
            )
        for role in ("prefill", "decode"):
            role_block = autoscaling.get(role)
            if role_block is None:
                continue
            if not isinstance(role_block, dict):
                raise ValueError(f"mooncake.autoscaling.{role} must be a mapping")
            _validate_role_autoscaling_bounds(role, role_block)


class InferenceManager:
    """Manages inference endpoints via the DynamoDB store."""

    def __init__(self, config: GCOConfig | None = None):
        self.config = config or get_config()
        self._aws_client = get_aws_client(config)

    def _get_store(self, region: str | None = None) -> InferenceEndpointStore:
        """Get an InferenceEndpointStore for the global region."""
        from gco.services.inference_store import InferenceEndpointStore

        # Use the global region for DynamoDB (same as job store)
        store_region = region or self.config.global_region
        return InferenceEndpointStore(region=store_region)

    def _build_mooncake_block(
        self,
        *,
        mode: str,
        prefill_replicas: int,
        decode_replicas: int,
        store: dict[str, Any] | None,
        transfer: dict[str, Any] | None,
        proxy: dict[str, Any] | None,
        autoscaling: dict[str, Any] | None,
        default_proxy_image: str | None = None,
    ) -> dict[str, Any]:
        """Assemble and validate an optional ``spec.mooncake`` block.

        Composes the topology and any supplied store/transfer/proxy/autoscaling
        sub-blocks into a single mapping, authoring store byte-size fields as
        base-10 integer decimal strings so they round-trip through DynamoDB,
        then validates the result. Validation is pure and runs before the
        caller persists anything, so a rejected block leaves any previously
        stored spec untouched. Raises :class:`ValueError` — naming the offending
        field — when the mode is unsupported or any field is invalid.

        The store-bearing modes (``store`` and ``both``) default the store to
        enabled so the shared master address is wired in (the ``both``-mode
        MultiConnector's store half depends on it), and split modes
        (``disaggregated`` and ``both``) default the prefill-decode proxy image
        to ``default_proxy_image`` when the caller supplies no explicit proxy
        image.
        """
        block: dict[str, Any] = {"mode": mode}

        # Split modes carry an XpYd topology; a single-instance store does not.
        if mode in _MOONCAKE_DISAGGREGATED_MODES:
            block["topology"] = {
                "prefill": prefill_replicas,
                "decode": decode_replicas,
            }

        # The store-bearing modes (store and both) only function with the KV
        # store enabled: the both-mode MultiConnector's store half is wired to
        # the shared master address, which the monitor renders only for an
        # enabled store. So a store block is always present for those modes,
        # defaulting enabled to True; an explicit store block still tunes
        # offload, sizes, and the cold tier.
        store_block = dict(store) if store is not None else None
        if mode in ("store", "both"):
            store_block = dict(store_block or {})
            store_block.setdefault("enabled", True)
        if store_block is not None:
            # Author byte-size fields as canonical decimal strings up front so
            # the persisted spec round-trips through DynamoDB without float or
            # Decimal coercion. Authoring also fails fast on bad inputs.
            for field in _MOONCAKE_STORE_BYTE_SIZE_FIELDS:
                if field in store_block:
                    try:
                        store_block[field] = author_byte_size(store_block[field])
                    except ValueError as exc:
                        raise ValueError(f"mooncake.store.{field}: {exc}") from exc
            block["store"] = store_block

        if transfer is not None:
            block["transfer"] = dict(transfer)

        # Split modes are fronted by the prefill-decode proxy, which needs a
        # container image. Default it to the same image the role pods serve from
        # (the upstream vLLM image bundles the reference proxy) so a split deploy
        # stands up without a separate proxy image; an explicit proxy image
        # still wins.
        proxy_block = dict(proxy) if proxy is not None else None
        if mode in _MOONCAKE_DISAGGREGATED_MODES and default_proxy_image:
            proxy_block = dict(proxy_block or {})
            proxy_block.setdefault("image", default_proxy_image)
        if proxy_block is not None:
            block["proxy"] = proxy_block

        if autoscaling is not None:
            block["autoscaling"] = dict(autoscaling)

        # Fail fast before persisting: rejects unsupported modes (naming the
        # allowed values) and every other invalid field.
        validate_mooncake_spec(block)
        return block

    def deploy(
        self,
        endpoint_name: str,
        image: str | None = None,
        target_regions: list[str] | None = None,
        replicas: int = 1,
        gpu_count: int = 1,
        gpu_type: str | None = None,
        port: int = 8000,
        model_path: str | None = None,
        model_source: str | None = None,
        health_check_path: str = "/health",
        env: dict[str, str] | None = None,
        namespace: str = "gco-inference",
        labels: dict[str, str] | None = None,
        autoscaling: dict[str, Any] | None = None,
        capacity_type: str | None = None,
        extra_args: list[str] | None = None,
        accelerator: str = "nvidia",
        node_selector: dict[str, str] | None = None,
        rewrite_image: bool = True,
        *,
        mooncake_mode: str | None = None,
        prefill_replicas: int = 1,
        decode_replicas: int = 1,
        mooncake_store: dict[str, Any] | None = None,
        mooncake_transfer: dict[str, Any] | None = None,
        mooncake_proxy: dict[str, Any] | None = None,
        mooncake_autoscaling: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """
        Deploy an inference endpoint to one or more regions.

        The endpoint spec is written to DynamoDB. The inference_monitor
        in each target region picks it up and creates the K8s resources.

        Args:
            endpoint_name: Unique name for the endpoint
            image: Container image (e.g. vllm/vllm-openai:v0.24.0). Optional
                when ``mooncake_mode`` is set: a disaggregated/store deploy
                with no image falls back to the default upstream
                Mooncake-enabled vLLM image. A plain deploy still requires an
                image.
            target_regions: Regions to deploy to (default: all deployed regions)
            replicas: Number of replicas per region
            gpu_count: GPUs per replica
            gpu_type: GPU instance type hint for node selector
            port: Container port
            model_path: EFS path for model weights
            health_check_path: Health check endpoint path
            env: Environment variables
            namespace: Kubernetes namespace
            labels: Labels for the endpoint
            rewrite_image: When True (the default), rewrite ECR URIs in
                ``image`` to target each region's local replica. Non-ECR
                refs (Docker Hub, GHCR, etc.) are left unchanged. When
                False, the URI is written verbatim to every region's
                spec — the operator is responsible for cross-region
                pulls. Per-region rewrites are stored under a
                ``region_overrides`` map on the spec keyed by region.
            mooncake_mode: When set to one of ``disaggregated``, ``store``,
                or ``both``, build and persist a ``spec.mooncake`` block for
                disaggregated prefill/decode serving and/or a shared KV-cache
                store. An unsupported value is rejected before anything is
                persisted.
            prefill_replicas: X in an XpYd topology — prefill instance count
                for split (``disaggregated``/``both``) modes.
            decode_replicas: Y in an XpYd topology — decode instance count for
                split modes.
            mooncake_store: Optional KV-store pool configuration merged into
                ``spec.mooncake.store``. Byte-size fields are authored as
                base-10 integer decimal strings so they round-trip through
                DynamoDB.
            mooncake_transfer: Optional Mooncake transfer intent and network
                device. ``protocol`` accepts ``rdma`` (the default; scheduled
                on EFA and rendered to vLLM as ``mooncake_protocol=efa``) or
                ``tcp`` (no EFA placement). ``device_name`` is forwarded to
                both the connector and mounted Mooncake configuration; an
                empty string lets Mooncake auto-detect it.
            mooncake_proxy: Optional PD proxy configuration merged into
                ``spec.mooncake.proxy``.
            mooncake_autoscaling: Optional per-role autoscaling configuration
                merged into ``spec.mooncake.autoscaling``.

        Returns:
            Created endpoint record
        """
        # Build the optional mooncake block first and validate it before any
        # persistence so a rejected block leaves any stored spec untouched. A
        # disaggregated/store deploy without an explicit image falls back to
        # the default upstream Mooncake-enabled vLLM image.
        mooncake_block: dict[str, Any] | None = None
        if mooncake_mode is not None:
            # Resolve the image before building the block so a split mode's
            # prefill-decode proxy can default to the same image the role pods
            # serve from (the upstream vLLM image bundles the reference proxy).
            if image is None:
                from .images import default_disaggregated_image

                image = default_disaggregated_image(config=self.config)
            mooncake_block = self._build_mooncake_block(
                mode=mooncake_mode,
                prefill_replicas=prefill_replicas,
                decode_replicas=decode_replicas,
                store=mooncake_store,
                transfer=mooncake_transfer,
                proxy=mooncake_proxy,
                autoscaling=mooncake_autoscaling,
                default_proxy_image=image,
            )

        if image is None:
            raise ValueError(
                "an image is required (pass image, or set mooncake_mode to use "
                "the default upstream Mooncake-enabled vLLM image)"
            )

        if not target_regions:
            stacks = self._aws_client.discover_regional_stacks()
            target_regions = list(stacks.keys())
            if not target_regions:
                raise ValueError("No deployed regions found. Deploy infrastructure first.")

        # Per-region image-URI rewrites for ECR refs. Each target region
        # gets the local replica's URI on its own spec, so the
        # inference_monitor's pod-spec materialiser pulls in-region
        # rather than across the WAN. Non-ECR URIs come back unchanged
        # from the helper, so this is a no-op for Docker Hub / GHCR refs.
        #
        # The helper lives in ``cli._image_uri`` rather than ``cli.images``
        # so this import doesn't create a module-level cycle:
        # ``cli.images`` itself imports the same helper. ``cli._image_uri``
        # is a leaf module with no project-side dependencies.
        region_image_map: dict[str, str] = {}
        if rewrite_image:
            from ._image_uri import rewrite_image_uri_for_region

            for region in target_regions:
                region_image_map[region] = rewrite_image_uri_for_region(image, region)

        spec = {
            "image": image,
            "port": port,
            "replicas": replicas,
            "gpu_count": gpu_count,
            "health_check_path": health_check_path,
        }
        # Preserve the rewrite map on the spec so the inference_monitor
        # service can pick the right URI per region when materialising
        # pods. When ``rewrite_image=False`` no map is set and the flat
        # ``image`` field is the only source.
        if region_image_map and any(uri != image for uri in region_image_map.values()):
            spec["region_image_uris"] = region_image_map
        if gpu_type:
            spec["gpu_type"] = gpu_type
        if model_path:
            spec["model_path"] = model_path
        if model_source:
            spec["model_source"] = model_source
        if env:
            spec["env"] = env
        if autoscaling:
            spec["autoscaling"] = autoscaling
        if capacity_type:
            spec["capacity_type"] = capacity_type
        if extra_args:
            spec["args"] = extra_args
        if accelerator != "nvidia":
            spec["accelerator"] = accelerator
        if node_selector:
            spec["node_selector"] = node_selector
        if mooncake_block is not None:
            spec["mooncake"] = mooncake_block

        store = self._get_store()
        result: dict[str, Any] = store.create_endpoint(
            endpoint_name=endpoint_name,
            spec=spec,
            target_regions=target_regions,
            namespace=namespace,
            labels=labels,
        )
        return result

    def list_endpoints(
        self,
        desired_state: str | None = None,
        region: str | None = None,
    ) -> list[dict[str, Any]]:
        """List all inference endpoints."""
        store = self._get_store()
        result: list[dict[str, Any]] = store.list_endpoints(
            desired_state=desired_state,
            target_region=region,
        )
        return result

    def get_endpoint(self, endpoint_name: str) -> dict[str, Any] | None:
        """Get details of a specific endpoint."""
        store = self._get_store()
        result: dict[str, Any] | None = store.get_endpoint(endpoint_name)
        return result

    def scale(self, endpoint_name: str, replicas: int) -> dict[str, Any] | None:
        """Scale an endpoint to a new replica count."""
        store = self._get_store()
        result: dict[str, Any] | None = store.scale_endpoint(endpoint_name, replicas)
        return result

    def set_topology(
        self,
        endpoint_name: str,
        prefill: int,
        decode: int,
    ) -> dict[str, Any] | None:
        """Resize a disaggregated endpoint's prefill/decode topology.

        Updates ``spec.mooncake.topology`` to the new XpYd counts and
        re-triggers reconciliation (via :meth:`InferenceEndpointStore.update_spec`,
        which flips ``desired_state`` to ``deploying``) so the per-region
        monitor adjusts the prefill and decode role replica counts.

        Both counts must be integers in the inclusive range
        ``[MOONCAKE_TOPOLOGY_MIN, MOONCAKE_TOPOLOGY_MAX]``. The counts are
        validated before anything is read or written, so a rejected request
        names the offending count and leaves the stored topology and
        ``desired_state`` untouched.

        Args:
            endpoint_name: Name of the disaggregated endpoint to resize.
            prefill: New prefill (X) instance count.
            decode: New decode (Y) instance count.

        Returns:
            The updated endpoint record, or ``None`` when no endpoint with
            ``endpoint_name`` exists.

        Raises:
            ValueError: when ``prefill`` or ``decode`` is not an integer in
                ``[MOONCAKE_TOPOLOGY_MIN, MOONCAKE_TOPOLOGY_MAX]``.
        """
        # Validate before any read or write so a bad count names the offending
        # field and leaves the stored topology and desired_state unchanged.
        for field, count in (("prefill", prefill), ("decode", decode)):
            if not _is_plain_int(count):
                raise ValueError(
                    f"topology {field} count must be an integer in "
                    f"[{MOONCAKE_TOPOLOGY_MIN}, {MOONCAKE_TOPOLOGY_MAX}], "
                    f"got {count!r}"
                )
            if not MOONCAKE_TOPOLOGY_MIN <= count <= MOONCAKE_TOPOLOGY_MAX:
                raise ValueError(
                    f"topology {field} count out of range "
                    f"[{MOONCAKE_TOPOLOGY_MIN}, {MOONCAKE_TOPOLOGY_MAX}]: {count}"
                )

        store = self._get_store()
        endpoint = store.get_endpoint(endpoint_name)
        if not endpoint:
            return None

        spec = endpoint.get("spec", {})
        # Preserve any existing mooncake sub-fields and replace only the
        # topology counts.
        mooncake = dict(spec.get("mooncake") or {})
        mooncake["topology"] = {"prefill": prefill, "decode": decode}
        spec["mooncake"] = mooncake

        result: dict[str, Any] | None = store.update_spec(endpoint_name, spec)
        return result

    def configure_store(
        self,
        endpoint_name: str,
        store_config: dict[str, Any],
    ) -> dict[str, Any] | None:
        """Update an endpoint's KV-cache store configuration.

        Merges ``store_config`` into ``spec.mooncake.store`` and re-triggers
        reconciliation (via :meth:`InferenceEndpointStore.update_spec`, which
        flips ``desired_state`` to ``deploying``) so the per-region monitor
        picks up the new store settings.

        Store byte-size fields are authored as base-10 integer decimal strings
        (so they round-trip through DynamoDB without float/Decimal coercion)
        and the resulting ``mooncake`` block is validated before anything is
        written. A rejected configuration names the offending field and leaves
        the stored spec untouched.

        Args:
            endpoint_name: Name of the endpoint to reconfigure.
            store_config: KV-store pool settings merged into
                ``spec.mooncake.store``.

        Returns:
            The updated endpoint record, or ``None`` when no endpoint with
            ``endpoint_name`` exists.

        Raises:
            ValueError: when the resulting ``mooncake`` block is invalid (for
                example an out-of-range byte-size field).
        """
        store = self._get_store()
        endpoint = store.get_endpoint(endpoint_name)
        if not endpoint:
            return None

        spec = endpoint.get("spec", {})
        # Preserve any existing mooncake sub-fields and replace only the store
        # block, authoring byte-size fields as canonical decimal strings.
        mooncake = dict(spec.get("mooncake") or {})
        store_block = dict(store_config)
        for field in _MOONCAKE_STORE_BYTE_SIZE_FIELDS:
            if field in store_block:
                try:
                    store_block[field] = author_byte_size(store_block[field])
                except ValueError as exc:
                    raise ValueError(f"mooncake.store.{field}: {exc}") from exc
        mooncake["store"] = store_block
        spec["mooncake"] = mooncake

        # Fail fast before persisting so a rejected block leaves the stored
        # spec untouched.
        validate_mooncake_spec(mooncake)

        result: dict[str, Any] | None = store.update_spec(endpoint_name, spec)
        return result

    def stop(self, endpoint_name: str) -> dict[str, Any] | None:
        """Stop an endpoint (scale to zero, keep resources)."""
        store = self._get_store()
        result: dict[str, Any] | None = store.update_desired_state(endpoint_name, "stopped")
        return result

    def start(self, endpoint_name: str) -> dict[str, Any] | None:
        """Start a stopped endpoint."""
        store = self._get_store()
        result: dict[str, Any] | None = store.update_desired_state(endpoint_name, "running")
        return result

    def delete(self, endpoint_name: str) -> dict[str, Any] | None:
        """Mark an endpoint for deletion (inference_monitor cleans up)."""
        store = self._get_store()
        result: dict[str, Any] | None = store.update_desired_state(endpoint_name, "deleted")
        return result

    def update_image(self, endpoint_name: str, image: str) -> dict[str, Any] | None:
        """Update the container image for an endpoint."""
        if not isinstance(image, str) or not image.strip():
            raise ValueError("Image must be a non-empty string")

        store = self._get_store()
        endpoint = store.get_endpoint(endpoint_name)
        if not endpoint:
            return None
        raw_spec = endpoint.get("spec")
        if not isinstance(raw_spec, dict):
            raise ValueError(f"Endpoint '{endpoint_name}' has an invalid spec")
        spec = deepcopy(raw_spec)
        spec["image"] = image.strip()
        # A direct image update is global. Stale regional rewrites would take
        # precedence in the monitor and silently keep serving the old image.
        spec.pop("region_image_uris", None)
        result: dict[str, Any] | None = store.update_spec(endpoint_name, spec)
        return result

    def add_region(self, endpoint_name: str, region: str) -> dict[str, Any] | None:
        """Add a region to an existing endpoint."""
        from datetime import UTC, datetime

        store = self._get_store()
        endpoint = store.get_endpoint(endpoint_name)
        if not endpoint:
            return None
        regions = endpoint.get("target_regions", [])
        if region not in regions:
            regions.append(region)
        # Update via raw DynamoDB update
        try:
            response = store._table.update_item(
                Key={"endpoint_name": endpoint_name},
                UpdateExpression="SET target_regions = :r, updated_at = :u",
                ExpressionAttributeValues={
                    ":r": regions,
                    ":u": datetime.now(UTC).isoformat(),
                },
                ReturnValues="ALL_NEW",
            )
            result: dict[str, Any] | None = response.get("Attributes")
            return result
        except Exception as e:
            logger.error("Failed to add region: %s", e)
            return None

    def remove_region(self, endpoint_name: str, region: str) -> dict[str, Any] | None:
        """Remove a region from an existing endpoint."""
        store = self._get_store()
        endpoint = store.get_endpoint(endpoint_name)
        if not endpoint:
            return None
        regions = endpoint.get("target_regions", [])
        if region in regions:
            regions.remove(region)
        try:
            from datetime import UTC, datetime

            response = store._table.update_item(
                Key={"endpoint_name": endpoint_name},
                UpdateExpression="SET target_regions = :r, updated_at = :u",
                ExpressionAttributeValues={
                    ":r": regions,
                    ":u": datetime.now(UTC).isoformat(),
                },
                ReturnValues="ALL_NEW",
            )
            result: dict[str, Any] | None = response.get("Attributes")
            return result
        except Exception as e:
            logger.error("Failed to remove region: %s", e)
            return None

    def canary_deploy(
        self,
        endpoint_name: str,
        image: str,
        weight: int = 10,
        replicas: int = 1,
    ) -> dict[str, Any] | None:
        """Start a canary deployment for an existing classic endpoint.

        Creates a canary variant with the new image receiving ``weight``%
        of traffic. Mooncake endpoints are excluded because their split-role
        topology cannot be represented by the classic canary Deployment.

        Args:
            endpoint_name: Existing endpoint to canary
            image: New container image for the canary
            weight: Percentage of traffic to route to canary (1-99)
            replicas: Positive number of canary replicas

        Returns:
            Updated endpoint record, or None if endpoint not found
        """
        if not isinstance(image, str) or not image.strip():
            raise ValueError("Canary image must be a non-empty string")
        if not _is_plain_int(weight) or not 1 <= weight <= 99:
            raise ValueError("Canary weight must be an integer between 1 and 99")
        if not _is_plain_int(replicas) or replicas < 1:
            raise ValueError("Canary replicas must be a positive integer")

        store = self._get_store()
        endpoint = store.get_endpoint(endpoint_name)
        if not endpoint:
            return None

        if endpoint.get("desired_state") not in ("running", "deploying"):
            raise ValueError(
                f"Cannot canary an endpoint in '{endpoint.get('desired_state')}' state. "
                "Endpoint must be running or deploying."
            )

        raw_spec = endpoint.get("spec")
        if not isinstance(raw_spec, dict):
            raise ValueError(f"Endpoint '{endpoint_name}' has an invalid spec")
        if "mooncake" in raw_spec:
            raise ValueError("Canary deployments are not supported for Mooncake endpoints")

        # Never mutate the object returned by the store; callers and test
        # doubles may retain it as shared state.
        spec = deepcopy(raw_spec)
        spec["canary"] = {
            "image": image.strip(),
            "weight": weight,
            "replicas": replicas,
        }

        result: dict[str, Any] | None = store.update_spec(endpoint_name, spec)
        return result

    def promote_canary(self, endpoint_name: str) -> dict[str, Any] | None:
        """Promote a classic canary to primary and remove its deployment."""
        store = self._get_store()
        endpoint = store.get_endpoint(endpoint_name)
        if not endpoint:
            return None

        raw_spec = endpoint.get("spec")
        if not isinstance(raw_spec, dict):
            raise ValueError(f"Endpoint '{endpoint_name}' has an invalid spec")
        if "mooncake" in raw_spec:
            raise ValueError("Canary promotion is not supported for Mooncake endpoints")

        canary = raw_spec.get("canary")
        if not isinstance(canary, dict):
            raise ValueError(f"Endpoint '{endpoint_name}' has no active canary deployment")
        if "image" not in canary:
            raise ValueError(
                f"Canary deployment for '{endpoint_name}' is missing the 'image' field"
            )
        canary_image = canary["image"]
        if not isinstance(canary_image, str) or not canary_image.strip():
            raise ValueError(
                f"Canary deployment for '{endpoint_name}' has an invalid 'image' field"
            )

        spec = deepcopy(raw_spec)
        spec["image"] = canary_image.strip()
        spec.pop("canary", None)
        # The canary image is explicit and global. Existing per-region primary
        # rewrites point at the superseded image and must not take precedence.
        spec.pop("region_image_uris", None)

        result: dict[str, Any] | None = store.update_spec(endpoint_name, spec)
        return result

    def rollback_canary(self, endpoint_name: str) -> dict[str, Any] | None:
        """Remove the canary deployment, keeping the primary unchanged."""
        store = self._get_store()
        endpoint = store.get_endpoint(endpoint_name)
        if not endpoint:
            return None

        raw_spec = endpoint.get("spec")
        if not isinstance(raw_spec, dict):
            raise ValueError(f"Endpoint '{endpoint_name}' has an invalid spec")
        if "canary" not in raw_spec:
            raise ValueError(f"Endpoint '{endpoint_name}' has no active canary deployment")

        # Rollback is deliberately allowed for a legacy invalid
        # Mooncake-plus-canary record so an operator can repair it.
        spec = deepcopy(raw_spec)
        spec.pop("canary", None)
        result: dict[str, Any] | None = store.update_spec(endpoint_name, spec)
        return result


def get_inference_manager(config: GCOConfig | None = None) -> InferenceManager:
    """Factory function for InferenceManager."""
    return InferenceManager(config)
