"""Per-role autoscaler bounds and the static replica fallback.

A disaggregated endpoint can carry an optional ``autoscaling`` block. When that
block is enabled, the monitor materializes one autoscaler per present role that
scales the ``{name}-{role}`` Deployment between the role's ``min_replicas`` and
``max_replicas``. When the block is absent or disabled, no autoscaler is created
and each role's replica count comes straight from the topology
(``topology.prefill`` for prefill, ``topology.decode`` for decode).

These examples pin both halves across a wide range of generated specs:

- With autoscaling enabled, every present role yields exactly one autoscaler
  whose ``min_replicas``/``max_replicas`` equal the spec and whose
  ``scale_target_ref`` names the ``{name}-{role}`` Deployment. The created
  autoscaler object is captured through a mocked autoscaling client.
- With autoscaling absent or disabled, no autoscaler is created for any role and
  the resolved replica count equals the topology value exactly.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock, patch

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

_SPLIT_MODES = ("disaggregated", "both")
_ROLES = ("prefill", "decode")


def _make_monitor(region: str = "us-east-1"):
    """Build an :class:`InferenceMonitor` with every K8s client mocked out."""
    with (
        patch("gco.services.inference_monitor.config.load_incluster_config"),
        patch("gco.services.inference_monitor.client.AppsV1Api") as mock_apps,
        patch("gco.services.inference_monitor.client.CoreV1Api") as mock_core,
        patch("gco.services.inference_monitor.client.NetworkingV1Api") as mock_net,
        patch("gco.services.inference_monitor.client.AutoscalingV2Api"),
    ):
        from gco.services.inference_monitor import InferenceMonitor

        monitor = InferenceMonitor(
            cluster_id="test-cluster",
            region=region,
            store=MagicMock(),
            namespace="gco-inference",
            reconcile_interval=5,
        )
        monitor.apps_v1 = mock_apps.return_value
        monitor.core_v1 = mock_core.return_value
        monitor.networking_v1 = mock_net.return_value
        return monitor


@st.composite
def _role_bounds(draw: st.DrawFn) -> dict[str, int]:
    """A role autoscaling block with a valid min/max pair."""
    low = draw(st.integers(min_value=1, max_value=50))
    high = draw(st.integers(min_value=low, max_value=low + 50))
    return {"min_replicas": low, "max_replicas": high}


@st.composite
def _enabled_spec(draw: st.DrawFn) -> dict[str, Any]:
    """A split-mode spec whose autoscaling block is enabled for some roles."""
    mode = draw(st.sampled_from(_SPLIT_MODES))
    topology = {
        "prefill": draw(st.integers(min_value=1, max_value=1000)),
        "decode": draw(st.integers(min_value=1, max_value=1000)),
    }
    autoscaling: dict[str, Any] = {"enabled": True}
    # At least one role must be present so the spec exercises a real autoscaler.
    present = draw(st.lists(st.sampled_from(_ROLES), min_size=1, unique=True))
    for role in present:
        autoscaling[role] = draw(_role_bounds())
    return {
        "mode": mode,
        "topology": topology,
        "autoscaling": autoscaling,
    }


@st.composite
def _static_spec(draw: st.DrawFn) -> dict[str, Any]:
    """A split-mode spec with no enabled autoscaling block."""
    mode = draw(st.sampled_from(_SPLIT_MODES))
    topology = {
        "prefill": draw(st.integers(min_value=1, max_value=1000)),
        "decode": draw(st.integers(min_value=1, max_value=1000)),
    }
    spec: dict[str, Any] = {"mode": mode, "topology": topology}
    # Either omit autoscaling entirely or include a disabled block.
    choice = draw(st.sampled_from(("absent", "disabled")))
    if choice == "disabled":
        block: dict[str, Any] = {"enabled": False}
        # A disabled block may still carry leftover role bounds; they are ignored.
        if draw(st.booleans()):
            block["prefill"] = draw(_role_bounds())
        if draw(st.booleans()):
            block["decode"] = draw(_role_bounds())
        spec["autoscaling"] = block
    return spec


@settings(
    max_examples=75,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)
@given(spec=_enabled_spec(), name=st.from_regex(r"[a-z][a-z0-9-]{0,12}", fullmatch=True))
def test_enabled_autoscaling_creates_bounded_per_role_autoscaler(spec, name):
    """Each present role yields one autoscaler matching its spec bounds.

    For every role carrying a bounds block, exactly one autoscaler is created
    whose lower and upper bounds equal the spec values and whose scale target is
    the ``{name}-{role}`` Deployment.
    """
    monitor = _make_monitor()
    autoscaling = spec["autoscaling"]
    endpoint_spec = {"mooncake": spec}

    for role in _ROLES:
        with patch("gco.services.inference_monitor.client.AutoscalingV2Api") as mock_api:
            create = mock_api.return_value.create_namespaced_horizontal_pod_autoscaler
            monitor._create_role_hpa(name, "gco-inference", endpoint_spec, role)

        if role not in autoscaling:
            # A role with no bounds block produces no autoscaler.
            create.assert_not_called()
            continue

        # Exactly one autoscaler is created for a present role.
        assert create.call_count == 1
        _, created_hpa = create.call_args.args
        bounds = autoscaling[role]
        target = f"{name}-{role}"

        assert created_hpa.metadata.name == target
        assert created_hpa.spec.min_replicas == bounds["min_replicas"]
        assert created_hpa.spec.max_replicas == bounds["max_replicas"]
        assert created_hpa.spec.scale_target_ref.name == target


@settings(max_examples=75, deadline=None)
@given(spec=_static_spec(), name=st.from_regex(r"[a-z][a-z0-9-]{0,12}", fullmatch=True))
def test_without_autoscaling_no_hpa_and_replicas_track_topology(spec, name):
    """Absent/disabled autoscaling creates no autoscaler and keeps topology counts.

    No autoscaler is created for either role, and the resolved replica count for
    each role equals its topology value exactly.
    """
    monitor = _make_monitor()
    topology = spec["topology"]
    endpoint_spec = {"mooncake": spec}

    for role in _ROLES:
        with patch("gco.services.inference_monitor.client.AutoscalingV2Api") as mock_api:
            create = mock_api.return_value.create_namespaced_horizontal_pod_autoscaler
            monitor._create_role_hpa(name, "gco-inference", endpoint_spec, role)
        create.assert_not_called()

    assert monitor._replica_count_for_role(spec, "prefill") == topology["prefill"]
    assert monitor._replica_count_for_role(spec, "decode") == topology["decode"]
