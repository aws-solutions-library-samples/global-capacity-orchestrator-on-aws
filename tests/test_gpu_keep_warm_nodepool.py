"""The keep-warm GPU NodePool and what the health monitor needs to record its idle time.

``47-nodepool-gpu-keep-warm.yaml`` is ``gpu-x86-pool`` with its own taint and
label and a ``consolidateAfter`` set from cdk.json ``gpu_keep_warm.window``.
These checks lock in what the placement code (``gco.job_admission``) and the
recorder (``gco.services.keep_warm_recorder``) rely on: the label and taint
they name, the instance families shared with ``gpu-x86-pool``, the disruption
budgets that keep reclaim within the window, and the Lease, Role and
environment the health monitor's recorder runs with.
"""

from __future__ import annotations

import importlib.util
import re
import sys
import types
from pathlib import Path
from typing import Any

import yaml

from gco.keep_warm import (
    IDLE_ANNOTATION,
    KEEP_WARM_INSTANCE_FAMILIES,
    KEEP_WARM_LABEL,
    KEEP_WARM_NODE_LABELS,
    KEEP_WARM_NODEPOOL,
    KEEP_WARM_TOLERATION,
    KEEP_WARM_VALUE,
    KEEP_WARM_WINDOW_PLACEHOLDER,
    KEEP_WARM_WINDOW_SECONDS_PLACEHOLDER,
    render_keep_warm_defaults,
)

ROOT = Path(__file__).resolve().parent.parent
MANIFESTS = ROOT / "lambda" / "kubectl-applier-simple" / "manifests"
POOL = MANIFESTS / "47-nodepool-gpu-keep-warm.yaml"
GPU_POOL = MANIFESTS / "40-nodepool-gpu-x86.yaml"
#: Karpenter's consolidateAfter pattern (karpenter.sh/v1 NodePool CRD).
KARPENTER_DURATION = re.compile(r"^(([0-9]+(s|m|h))+|Never)$")


def _documents(path: Path) -> list[dict[str, Any]]:
    text = re.sub(r"\{\{[A-Z0-9_]+\}\}", "PLACEHOLDER", path.read_text(encoding="utf-8"))
    return [document for document in yaml.safe_load_all(text) if document]


def _nodepool(path: Path, *, render: bool = False) -> dict[str, Any]:
    text = path.read_text(encoding="utf-8")
    if render:
        text = render_keep_warm_defaults(text)
    return next(d for d in yaml.safe_load_all(text) if d and d.get("kind") == "NodePool")


def _requirements(pool: dict[str, Any]) -> dict[str, list[str]]:
    return {
        requirement["key"]: requirement["values"]
        for requirement in pool["spec"]["template"]["spec"]["requirements"]
    }


class TestKeepWarmNodePool:
    def test_name_label_and_taints_are_the_ones_the_code_names(self) -> None:
        pool = _nodepool(POOL)
        template = pool["spec"]["template"]

        assert pool["metadata"]["name"] == KEEP_WARM_NODEPOOL
        assert template["metadata"]["labels"][KEEP_WARM_LABEL] == KEEP_WARM_VALUE
        assert {
            (taint["key"], taint["value"], taint["effect"]) for taint in template["spec"]["taints"]
        } == {
            ("nvidia.com/gpu", "true", "NoSchedule"),
            (KEEP_WARM_TOLERATION["key"], KEEP_WARM_TOLERATION["value"], "NoSchedule"),
        }

    def test_offers_exactly_the_gpu_x86_pool_instances(self) -> None:
        """A keep-warm Job may ask for any instance a regular GPU Job can get."""
        assert _requirements(_nodepool(POOL)) == _requirements(_nodepool(GPU_POOL))
        assert _nodepool(POOL)["spec"]["limits"] == _nodepool(GPU_POOL)["spec"]["limits"]

    def test_the_labels_admission_judges_selectors_by_are_the_pools(self) -> None:
        """``KEEP_WARM_NODE_LABELS`` is every label value the pool fixes on its nodes."""
        pool = _nodepool(POOL)
        requirements = pool["spec"]["template"]["spec"]["requirements"]
        assert {requirement["operator"] for requirement in requirements} == {"In"}
        expected = {
            key: frozenset({value})
            for key, value in pool["spec"]["template"]["metadata"]["labels"].items()
        }
        expected.update({key: frozenset(values) for key, values in _requirements(pool).items()})
        expected["karpenter.sh/nodepool"] = frozenset({pool["metadata"]["name"]})

        assert expected == KEEP_WARM_NODE_LABELS
        assert expected["eks.amazonaws.com/instance-family"] == KEEP_WARM_INSTANCE_FAMILIES
        # Only SageMaker HyperPod nodes carry the capacity-provider label.
        assert "gco.aws/capacity-provider" not in expected

    def test_only_this_pool_carries_the_keep_warm_label(self) -> None:
        for path in sorted(MANIFESTS.glob("4*-nodepool-*.yaml")):
            labels = _nodepool(path)["spec"]["template"]["metadata"]["labels"]
            assert (KEEP_WARM_LABEL in labels) is (path == POOL), path.name

    def test_empty_nodes_wait_for_the_configured_window(self) -> None:
        disruption = _nodepool(POOL)["spec"]["disruption"]
        assert disruption["consolidationPolicy"] == "WhenEmpty"
        assert disruption["consolidateAfter"] == KEEP_WARM_WINDOW_PLACEHOLDER

        rendered = _nodepool(POOL, render=True)["spec"]["disruption"]["consolidateAfter"]
        assert rendered == "10m"
        assert KARPENTER_DURATION.match(rendered)

    def test_reclaim_of_empty_nodes_is_unbudgeted_and_every_other_reason_is_gradual(self) -> None:
        budgets = _nodepool(POOL)["spec"]["disruption"]["budgets"]
        by_reason = {reason: budget["nodes"] for budget in budgets for reason in budget["reasons"]}
        # Every budget names its reasons: one without would cap Empty too.
        assert all(budget.get("reasons") for budget in budgets)
        assert by_reason == {
            "Empty": "100%",
            "Underutilized": "10%",
            "Drifted": "10%",
            "NodeNotHealthy": "10%",
        }


class TestNodeAgentsRunOnKeepWarmNodes:
    """The pool's own taint must not keep the node agents off its nodes.

    A keep-warm node is a ``gpu-x86-pool`` node behind one more taint, so
    every DaemonSet that tolerates the GPU taints to run on GPU nodes
    tolerates this one too: the EKS add-ons' node agents and node-exporter
    (``GCORegionalStack._ADDON_NODE_TOLERATIONS``) and the EFA device plugin.
    The DCGM exporter is held to every NVIDIA pool's taints in
    ``tests/test_cluster_observability_charts.py``.
    """

    @staticmethod
    def _shipped_taints() -> list[dict[str, str]]:
        return [
            taint
            for path in sorted(MANIFESTS.glob("4*-nodepool-*.yaml"))
            for taint in _nodepool(path)["spec"]["template"]["spec"].get("taints", [])
        ]

    @staticmethod
    def _tolerated(tolerations: list[dict[str, str]], taint: dict[str, str]) -> bool:
        return any(
            toleration.get("key") == taint["key"]
            and toleration.get("operator") == "Exists"
            and toleration.get("effect") in (None, taint["effect"])
            for toleration in tolerations
        )

    def test_the_shipped_pools_carry_the_keep_warm_taint(self) -> None:
        assert KEEP_WARM_LABEL in {taint["key"] for taint in self._shipped_taints()}

    def test_the_add_on_node_agents_tolerate_every_nodepool_taint(self) -> None:
        from gco.stacks.regional_stack import GCORegionalStack

        for taint in self._shipped_taints():
            assert self._tolerated(GCORegionalStack._ADDON_NODE_TOLERATIONS, taint), taint

    def test_the_efa_device_plugin_tolerates_every_nodepool_taint(self) -> None:
        charts = yaml.safe_load(
            (ROOT / "lambda" / "helm-installer" / "charts.yaml").read_text(encoding="utf-8")
        )
        tolerations = charts["charts"]["aws-efa-device-plugin"]["values"]["tolerations"]
        for taint in self._shipped_taints():
            assert self._tolerated(tolerations, taint), taint


class TestHealthMonitorAccess:
    def test_the_recorder_lease_is_pre_created_and_writable_by_name_only(self) -> None:
        documents = _documents(MANIFESTS / "02-rbac.yaml")
        leases = {d["metadata"]["name"] for d in documents if d["kind"] == "Lease"}
        assert "gco-health-monitor-keep-warm" in leases

        role = next(
            d
            for d in documents
            if d["kind"] == "Role" and d["metadata"]["name"] == "gco-health-monitor-self-healing"
        )
        lease_rule = next(rule for rule in role["rules"] if rule["resources"] == ["leases"])
        assert "gco-health-monitor-keep-warm" in lease_rule["resourceNames"]
        assert lease_rule["verbs"] == ["get", "update"]

    def test_the_recorder_may_read_and_patch_jobs_in_gco_jobs_only(self) -> None:
        documents = _documents(MANIFESTS / "02-rbac.yaml")
        role = next(
            d
            for d in documents
            if d["kind"] == "Role"
            and d["metadata"]["name"] == "gco-health-monitor-keep-warm-records"
        )
        binding = next(
            d
            for d in documents
            if d["kind"] == "RoleBinding"
            and d["metadata"]["name"] == "gco-health-monitor-keep-warm-records"
        )
        assert role["metadata"]["namespace"] == binding["metadata"]["namespace"] == "gco-jobs"
        assert role["rules"] == [
            {"apiGroups": ["batch"], "resources": ["jobs"], "verbs": ["get", "patch"]}
        ]
        assert binding["roleRef"]["name"] == role["metadata"]["name"]
        assert binding["subjects"] == [
            {"kind": "ServiceAccount", "name": "gco-health-monitor-sa", "namespace": "gco-system"}
        ]

    def test_the_cluster_reads_the_recorder_makes_are_granted(self) -> None:
        documents = _documents(MANIFESTS / "02-rbac.yaml")
        cluster_role = next(
            d
            for d in documents
            if d["kind"] == "ClusterRole" and d["metadata"]["name"] == "gco-health-monitor-role"
        )
        granted = {
            (resource, verb)
            for rule in cluster_role["rules"]
            for resource in rule["resources"]
            for verb in rule["verbs"]
        }
        assert {("nodes", "list"), ("pods", "list"), ("jobs", "list")} <= granted

    def test_the_health_monitor_gets_the_lease_and_window(self) -> None:
        deployment = next(
            d for d in _documents(MANIFESTS / "30-health-monitor.yaml") if d["kind"] == "Deployment"
        )
        (container,) = [
            c
            for c in deployment["spec"]["template"]["spec"]["containers"]
            if c["name"] == "health-monitor"
        ]
        env = {item["name"]: item.get("value") for item in container["env"]}
        assert env["KEEP_WARM_LEASE_NAME"] == "gco-health-monitor-keep-warm"
        assert env["KEEP_WARM_LEASE_DURATION"] == "90"
        text = (MANIFESTS / "30-health-monitor.yaml").read_text(encoding="utf-8")
        assert f'value: "{KEEP_WARM_WINDOW_SECONDS_PLACEHOLDER}"' in text
        assert render_keep_warm_defaults(KEEP_WARM_WINDOW_SECONDS_PLACEHOLDER) == "600"


class TestSchemaValidationStub:
    def test_the_window_renders_to_a_duration_karpenter_accepts(self) -> None:
        path = ROOT / ".github" / "scripts" / "validate_k8s_manifests.py"
        spec = importlib.util.spec_from_file_location("validate_k8s_manifests_kw", path)
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)

        rendered = module.render_placeholders(POOL.read_text(encoding="utf-8"))

        pool = next(d for d in yaml.safe_load_all(rendered) if d and d["kind"] == "NodePool")
        assert KARPENTER_DURATION.match(pool["spec"]["disruption"]["consolidateAfter"])


class TestRegionalStackWindow:
    """``GCORegionalStack._gpu_keep_warm_config``, without a synth."""

    @staticmethod
    def _resolve(config: Any, context: dict[str, Any]) -> dict[str, Any]:
        from gco.stacks.regional_stack import GCORegionalStack

        stack = types.SimpleNamespace(
            config=config,
            node=types.SimpleNamespace(try_get_context=lambda key: context.get(key)),
        )
        return GCORegionalStack._gpu_keep_warm_config(stack)  # type: ignore[arg-type]

    def test_the_config_loader_getter_wins(self) -> None:
        config = types.SimpleNamespace(
            get_gpu_keep_warm_config=lambda: {"window": "15m", "window_seconds": 900}
        )
        assert self._resolve(config, {"gpu_keep_warm": {"window": "1h"}})["window"] == "15m"

    def test_a_config_without_the_getter_reads_the_stack_context(self) -> None:
        config = types.SimpleNamespace(get_gpu_keep_warm_config=lambda: None)
        resolved = self._resolve(config, {"gpu_keep_warm": {"window": "1h"}})
        assert resolved == {"window": "1h", "window_seconds": 3600}
        assert self._resolve(types.SimpleNamespace(), {})["window"] == "10m"


def test_the_idle_annotation_lives_under_the_project_prefix() -> None:
    """The annotation, label and taint share the gco.aws/ prefix the platform uses."""
    assert IDLE_ANNOTATION.startswith("gco.aws/")
    assert KEEP_WARM_LABEL.startswith("gco.aws/")
