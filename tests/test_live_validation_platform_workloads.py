"""
Tests for the live-validation platform-workloads action, its checks module,
and the kubectl plumbing the cluster-facing checks share.

Covers the cdk.json-derived expectations (cost-monitor only when cost
monitoring is configured, the manifest-processor HPA only when opted in, the
enforcement switch value), the per-object snapshots read through a scripted
kubectl (Deployments with their live pods, PodDisruptionBudgets, HPAs, the
``amazon-vpc-cni`` ConfigMap), the split between contract breaches that fail
immediately and transient states that are polled to a deadline, the action's
per-Region tunnel session and evidence shape, and ``checks/cluster.py``'s
fail-closed JSON reads and isolated-kubeconfig guard. Every kubectl, tunnel,
and clock boundary is faked.
"""

from __future__ import annotations

import contextlib
import importlib.util
import json
import re
import threading
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pytest
import yaml

from scripts.live_release_validation.actions import platform_workloads as action_module
from scripts.live_release_validation.checks import cluster as checks_cluster
from scripts.live_release_validation.checks import platform_workloads as checks

REGION = "us-east-1"
ALL_DEPLOYMENTS = (*checks.PLATFORM_DEPLOYMENTS, checks.COST_MONITOR_DEPLOYMENT)
APPLIER_DIR = Path(__file__).resolve().parents[1] / "lambda" / "kubectl-applier-simple"
#: Manifest placeholders parse as YAML flow mappings; neutralize them before loading.
_PLACEHOLDER = re.compile(r"\{\{[A-Z0-9_]+\}\}")


class _Clock:
    """Deterministic ``time`` replacement: sleeping advances the clock."""

    def __init__(self, start: float = 1_000.0, *, step: float = 1.0) -> None:
        self.now = start
        self.step = step
        self.sleeps: list[float] = []

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(float(seconds))
        self.now += max(float(seconds), self.step)


def _context(
    *,
    cdk_context: dict[str, Any] | None = None,
    regions: tuple[str, ...] = (REGION,),
) -> SimpleNamespace:
    settings = SimpleNamespace(
        run_id="run-123",
        poll_interval_seconds=0,
        command_timeout_seconds=30,
        repo_root=Path("/repo"),
        report_dir=Path("/private"),
        kubeconfig_path=Path("/private/kubeconfig"),
    )
    return SimpleNamespace(
        settings=settings,
        checkpoint=SimpleNamespace(state={}),
        state_lock=threading.RLock(),
        deployment_regions=regions,
        config=SimpleNamespace(project_name="gco-live"),
        cdk_context={} if cdk_context is None else cdk_context,
        persist=MagicMock(),
    )


def _deployment(
    name: str,
    *,
    replicas: int = 2,
    ready: int | None = None,
    available: int | None = None,
    updated: int | None = None,
    generation: int = 3,
    observed: int | None = None,
) -> dict[str, Any]:
    return {
        "metadata": {"name": name, "generation": generation},
        "spec": {"replicas": replicas},
        "status": {
            "readyReplicas": replicas if ready is None else ready,
            "availableReplicas": replicas if available is None else available,
            "updatedReplicas": replicas if updated is None else updated,
            "observedGeneration": generation if observed is None else observed,
        },
    }


def _pod(
    name: str,
    *,
    phase: str = "Running",
    ready: bool = True,
    restarts: int = 0,
    init_restarts: int | None = None,
    deleting: bool = False,
    containers: int = 1,
) -> dict[str, Any]:
    metadata: dict[str, Any] = {"name": name}
    if deleting:
        metadata["deletionTimestamp"] = "2026-09-10T00:00:00Z"
    status: dict[str, Any] = {
        "phase": phase,
        "containerStatuses": [
            {"name": f"c{index}", "ready": ready, "restartCount": restarts}
            for index in range(containers)
        ],
    }
    if init_restarts is not None:
        status["initContainerStatuses"] = [{"name": "init", "restartCount": init_restarts}]
    return {"metadata": metadata, "status": status}


def _budget(
    name: str, *, allowed: int = 1, max_unavailable: Any = 1, healthy: int = 2
) -> dict[str, Any]:
    return {
        "metadata": {"name": name},
        "spec": {"maxUnavailable": max_unavailable},
        "status": {
            "disruptionsAllowed": allowed,
            "currentHealthy": healthy,
            "desiredHealthy": healthy - 1,
            "expectedPods": healthy,
        },
    }


def _hpa(
    name: str, target: str, *, able: bool = True, active: bool = True, replicas: int = 3
) -> dict[str, Any]:
    return {
        "metadata": {"name": name},
        "spec": {
            "scaleTargetRef": {"kind": "Deployment", "name": target},
            "minReplicas": 3,
            "maxReplicas": 10,
        },
        "status": {
            "currentReplicas": replicas,
            "desiredReplicas": replicas,
            "conditions": [
                {"type": "AbleToScale", "status": "True" if able else "False"},
                {"type": "ScalingActive", "status": "True" if active else "False"},
                "not-a-condition",
            ],
        },
    }


def _switch(value: str = "true") -> dict[str, Any]:
    return {
        "metadata": {"name": checks.ENFORCEMENT_SWITCH_NAME},
        "data": dict.fromkeys(checks.ENFORCEMENT_SWITCH_KEYS, value),
    }


class _Cluster:
    """A scripted kubectl: namespaced objects by kind/name, pod lists by selector."""

    def __init__(self) -> None:
        self.objects: dict[tuple[str, str, str], Any] = {}
        self.pods: dict[tuple[str, str], list[dict[str, Any]]] = {}
        self.failures: dict[tuple[str, str, str], tuple[int, str, str]] = {}
        self.calls: list[tuple[str, ...]] = []

    def put(self, kind: str, obj: dict[str, Any], namespace: str = checks.PLATFORM_NAMESPACE):
        self.objects[(kind, namespace, obj["metadata"]["name"])] = obj

    def __call__(self, *args: str, timeout: float, **kwargs: Any) -> tuple[int, str, str]:
        assert timeout == 30.0
        assert not kwargs
        self.calls.append(args)
        assert args[0] == "get" and args[-2:] == ("--output", "json")
        kind = args[1]
        namespace = args[args.index("--namespace") + 1]
        if kind == "pods":
            selector = args[args.index("--selector") + 1]
            return 0, json.dumps({"items": self.pods.get((namespace, selector), [])}), ""
        key = (kind, namespace, args[2])
        if key in self.failures:
            return self.failures[key]
        obj = self.objects.get(key)
        if obj is None:
            return 1, "", f'Error from server (NotFound): {kind} "{args[2]}" not found'
        return 0, json.dumps(obj), ""


def _healthy_cluster(deployments: tuple[str, ...] = checks.PLATFORM_DEPLOYMENTS) -> _Cluster:
    cluster = _Cluster()
    for name in deployments:
        cluster.put("deployment", _deployment(name))
        cluster.pods[(checks.PLATFORM_NAMESPACE, f"app={name}")] = [
            _pod(f"{name}-a"),
            _pod(f"{name}-b"),
        ]
    for name in checks.DISRUPTION_BUDGETS:
        cluster.put("poddisruptionbudget", _budget(name))
    cluster.put("horizontalpodautoscaler", _hpa(*checks.INFERENCE_PROXY_AUTOSCALER))
    cluster.put("configmap", _switch(), namespace=checks.ENFORCEMENT_SWITCH_NAMESPACE)
    return cluster


def _install_clock(monkeypatch: pytest.MonkeyPatch, **kwargs: Any) -> _Clock:
    clock = _Clock(**kwargs)
    monkeypatch.setattr(checks, "time", clock)
    return clock


class TestConfiguredExpectations:
    def test_cost_monitor_joins_the_deployment_set_only_when_configured(self) -> None:
        assert checks.expected_deployments(_context()) == ALL_DEPLOYMENTS
        disabled = _context(cdk_context={"cost_monitoring": {"enabled": False}})
        assert checks.expected_deployments(disabled) == checks.PLATFORM_DEPLOYMENTS
        no_observability = _context(cdk_context={"cluster_observability": {"enabled": False}})
        assert checks.expected_deployments(no_observability) == checks.PLATFORM_DEPLOYMENTS

    @pytest.mark.parametrize(
        ("cdk_context", "expected"),
        [
            ({}, False),
            ({"manifest_processor": "not-a-block"}, False),
            ({"manifest_processor": {"replicas": 3}}, False),
            ({"manifest_processor": {"autoscaling": {"max_replicas": 6}}}, False),
            ({"manifest_processor": {"autoscaling": {"enabled": False}}}, False),
            ({"manifest_processor": {"autoscaling": {"enabled": True}}}, True),
        ],
    )
    def test_manifest_processor_autoscaling_defaults_off(
        self, cdk_context: dict[str, Any], expected: bool
    ) -> None:
        assert checks.manifest_processor_autoscaling_enabled(_context(cdk_context=cdk_context)) is (
            expected
        )

    @pytest.mark.parametrize(
        ("cdk_context", "expected"),
        [
            ({}, True),
            ({"eks_cluster": {"endpoint_access": "PRIVATE"}}, True),
            ({"eks_cluster": {"network_policy_enforcement": True}}, True),
            ({"eks_cluster": {"network_policy_enforcement": False}}, False),
        ],
    )
    def test_network_policy_enforcement_defaults_on(
        self, cdk_context: dict[str, Any], expected: bool
    ) -> None:
        assert checks.network_policy_enforcement_enabled(_context(cdk_context=cdk_context)) is (
            expected
        )


class TestKubectlJson:
    def test_parses_json_and_appends_the_output_flag(self) -> None:
        record: dict[str, Any] = {}
        kubectl = MagicMock(return_value=(0, '{"kind": "Pod"}', ""))
        assert checks_cluster.kubectl_json(kubectl, record, "get", "pod", "x", timeout=5.0) == {
            "kind": "Pod"
        }
        kubectl.assert_called_once_with("get", "pod", "x", "--output", "json", timeout=5.0)
        assert record == {}

    @pytest.mark.parametrize(
        "stderr",
        ['Error from server (NotFound): pods "x" not found', "error: NotFound"],
    )
    def test_absent_objects_read_as_none(self, stderr: str) -> None:
        kubectl = MagicMock(return_value=(1, "", stderr))
        assert checks_cluster.kubectl_json(kubectl, {}, "get", "pod", "x", timeout=5.0) is None

    def test_other_failures_are_recorded_and_raised(self) -> None:
        record: dict[str, Any] = {}
        kubectl = MagicMock(
            return_value=(1, "partial", "Error from server (Forbidden): " + "x" * 2000)
        )
        with pytest.raises(checks_cluster.KubectlError, match="kubectl get pod failed with exit 1"):
            checks_cluster.kubectl_json(kubectl, record, "get", "pod", "x", timeout=5.0)
        error = record["last_kubectl_error"]
        assert error["argv"] == ["get", "pod", "x"]
        assert error["returncode"] == 1
        assert error["stdout"] == "partial"
        assert len(error["stderr"]) == 1_000
        assert error["stderr"].endswith("x")

    def test_invalid_json_is_recorded_and_raised(self) -> None:
        record: dict[str, Any] = {}
        kubectl = MagicMock(return_value=(0, "not json", ""))
        with pytest.raises(checks_cluster.KubectlError, match="returned invalid JSON"):
            checks_cluster.kubectl_json(kubectl, record, "get", "pod", "x", timeout=5.0)
        error = record["last_kubectl_error"]
        assert error["stdout"] == "not json"
        assert error["error"].startswith("JSONDecodeError")


class TestClusterKubectl:
    def test_opens_the_tunnelled_session_through_the_isolated_kubeconfig(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        ctx = _context()
        seen: dict[str, Any] = {}
        sentinel = object()

        @contextlib.contextmanager
        def cluster_session(*args: Any, **kwargs: Any):
            seen["args"] = args
            seen["kwargs"] = kwargs
            yield sentinel

        monkeypatch.setattr(checks_cluster.kube, "cluster_session", cluster_session)
        with checks_cluster.cluster_kubectl(ctx, "eu-west-1") as kubectl:
            assert kubectl is sentinel
        assert seen["args"] == (Path("/repo"), "gco-live-eu-west-1", "eu-west-1")
        assert seen["kwargs"]["kubeconfig_path"] == Path("/private/kubeconfig")
        assert seen["kwargs"]["gco_command"] == (
            checks_cluster.sys.executable,
            "-m",
            "cli.main",
        )

    def test_refuses_a_kubeconfig_outside_the_private_report_dir(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        ctx = _context()
        ctx.settings.kubeconfig_path = Path("/tmp/kubeconfig")
        session = MagicMock()
        monkeypatch.setattr(checks_cluster.kube, "cluster_session", session)
        with (
            pytest.raises(checks_cluster.KubectlError, match="escaped the private report dir"),
            checks_cluster.cluster_kubectl(ctx, REGION),
        ):
            pass  # pragma: no cover - the guard raises before the body runs
        session.assert_not_called()


class TestConvergedSnapshot:
    def test_healthy_cluster_returns_the_snapshot_without_waiting(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        clock = _install_clock(monkeypatch)
        ctx = _context(cdk_context={"cost_monitoring": {"enabled": False}})
        cluster = _healthy_cluster()

        snapshot = checks.verify_platform_workloads(ctx, REGION, cluster)

        assert snapshot["violations"] == []
        assert snapshot["pending"] == []
        assert set(snapshot["deployments"]) == set(checks.PLATFORM_DEPLOYMENTS)
        health_monitor = snapshot["deployments"]["health-monitor"]
        assert health_monitor["converged"] is True
        assert health_monitor["restarts"] == 0
        assert [pod["name"] for pod in health_monitor["pods"]] == [
            "health-monitor-a",
            "health-monitor-b",
        ]
        assert snapshot["budgets"]["inference-proxy-pdb"]["disruptions_allowed"] == 1
        assert snapshot["autoscalers"]["inference-proxy-hpa"]["target"] == "inference-proxy"
        assert snapshot["autoscalers"]["manifest-processor-hpa"] == {"exists": False}
        assert snapshot["enforcement_switch"]["values"] == dict.fromkeys(
            checks.ENFORCEMENT_SWITCH_KEYS, "true"
        )
        assert clock.sleeps == []
        record = ctx.checkpoint.state["platform_workloads"][REGION]
        assert record["last_snapshot"] is snapshot
        assert record["observations"] == 1
        ctx.persist.assert_called_once_with()

    def test_cost_monitor_and_opted_in_hpa_join_the_contract(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _install_clock(monkeypatch)
        ctx = _context(
            cdk_context={
                "manifest_processor": {"autoscaling": {"enabled": True}},
                "eks_cluster": {"network_policy_enforcement": False},
            }
        )
        cluster = _healthy_cluster(ALL_DEPLOYMENTS)
        cluster.put("horizontalpodautoscaler", _hpa(*checks.MANIFEST_PROCESSOR_AUTOSCALER))
        cluster.put("configmap", _switch("false"), namespace=checks.ENFORCEMENT_SWITCH_NAMESPACE)

        snapshot = checks.verify_platform_workloads(ctx, REGION, cluster)

        assert set(snapshot["deployments"]) == set(ALL_DEPLOYMENTS)
        assert snapshot["autoscalers"]["manifest-processor-hpa"]["exists"] is True
        assert snapshot["manifest_processor_autoscaling"] is True
        assert snapshot["network_policy_enforcement"] is False
        assert snapshot["violations"] == []

    def test_terminating_pods_are_not_part_of_the_service(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _install_clock(monkeypatch)
        ctx = _context(cdk_context={"cost_monitoring": {"enabled": False}})
        cluster = _healthy_cluster()
        cluster.pods[(checks.PLATFORM_NAMESPACE, "app=inference-proxy")].append(
            _pod("inference-proxy-old", ready=False, restarts=7, deleting=True)
        )

        snapshot = checks.verify_platform_workloads(ctx, REGION, cluster)

        proxy = snapshot["deployments"]["inference-proxy"]
        assert len(proxy["pods"]) == 2
        assert proxy["restarts"] == 0
        assert proxy["converged"] is True

    def test_malformed_payloads_read_as_empty_rather_than_crashing(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _install_clock(monkeypatch)
        ctx = _context(cdk_context={"cost_monitoring": {"enabled": False}})
        cluster = _healthy_cluster()
        cluster.put(
            "deployment",
            {"metadata": {"name": "health-monitor"}, "spec": "bogus", "status": None},
        )
        cluster.pods[(checks.PLATFORM_NAMESPACE, "app=health-monitor")] = [
            "not-a-pod",
            {"metadata": None, "status": {"phase": "Running", "containerStatuses": "bogus"}},
        ]
        cluster.put(
            "horizontalpodautoscaler",
            {
                "metadata": {"name": "inference-proxy-hpa"},
                "spec": None,
                "status": {"conditions": 3},
            },
        )
        cluster.put("poddisruptionbudget", {"metadata": {"name": "health-monitor-pdb"}})
        cluster.put(
            "configmap",
            {"metadata": {"name": "amazon-vpc-cni"}},
            namespace=checks.ENFORCEMENT_SWITCH_NAMESPACE,
        )

        with pytest.raises(checks.PlatformWorkloadValidationError) as info:
            checks.verify_platform_workloads(ctx, REGION, cluster)

        message = str(info.value)
        assert "HorizontalPodAutoscaler inference-proxy-hpa targets None" in message
        assert "PodDisruptionBudget health-monitor-pdb has maxUnavailable None" in message
        assert "key enable-network-policy-controller is None" in message
        snapshot = ctx.checkpoint.state["platform_workloads"][REGION]["last_snapshot"]
        health_monitor = snapshot["deployments"]["health-monitor"]
        assert health_monitor["desired"] == 0
        assert [pod["ready"] for pod in health_monitor["pods"]] == [False, False]


class TestContractBreaches:
    """Breaches fail on the first observation; waiting could not heal them."""

    @staticmethod
    def _run(cluster: _Cluster, monkeypatch: pytest.MonkeyPatch, **cdk: Any) -> str:
        clock = _install_clock(monkeypatch)
        ctx = _context(cdk_context={"cost_monitoring": {"enabled": False}, **cdk})
        with pytest.raises(checks.PlatformWorkloadValidationError) as info:
            checks.verify_platform_workloads(ctx, REGION, cluster)
        assert clock.sleeps == []
        assert "breach the hosting contract" in str(info.value)
        return str(info.value)

    def test_missing_deployment(self, monkeypatch: pytest.MonkeyPatch) -> None:
        cluster = _healthy_cluster()
        del cluster.objects[("deployment", checks.PLATFORM_NAMESPACE, "manifest-processor")]
        message = self._run(cluster, monkeypatch)
        assert "Deployment manifest-processor is missing" in message

    def test_restarted_container_including_init_containers(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        cluster = _healthy_cluster()
        cluster.pods[(checks.PLATFORM_NAMESPACE, "app=health-monitor")] = [
            _pod("health-monitor-a", restarts=2, containers=2),
            _pod("health-monitor-b", init_restarts=1),
        ]
        message = self._run(cluster, monkeypatch)
        assert "Deployment health-monitor has 5 container restart(s)" in message

    def test_failed_pod(self, monkeypatch: pytest.MonkeyPatch) -> None:
        cluster = _healthy_cluster()
        cluster.pods[(checks.PLATFORM_NAMESPACE, "app=inference-monitor")].append(
            _pod("inference-monitor-evicted", phase="Failed", ready=False)
        )
        message = self._run(cluster, monkeypatch)
        assert "Deployment inference-monitor has failed pod(s): ['inference-monitor-evicted']" in (
            message
        )

    def test_missing_or_misshapen_budget(self, monkeypatch: pytest.MonkeyPatch) -> None:
        cluster = _healthy_cluster()
        del cluster.objects[
            ("poddisruptionbudget", checks.PLATFORM_NAMESPACE, "health-monitor-pdb")
        ]
        cluster.put("poddisruptionbudget", _budget("inference-proxy-pdb", max_unavailable="50%"))
        message = self._run(cluster, monkeypatch)
        assert "PodDisruptionBudget health-monitor-pdb is missing" in message
        assert "PodDisruptionBudget inference-proxy-pdb has maxUnavailable '50%', expected 1" in (
            message
        )

    def test_string_one_is_an_acceptable_budget(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _install_clock(monkeypatch)
        cluster = _healthy_cluster()
        cluster.put("poddisruptionbudget", _budget("inference-proxy-pdb", max_unavailable="1"))
        ctx = _context(cdk_context={"cost_monitoring": {"enabled": False}})
        assert checks.verify_platform_workloads(ctx, REGION, cluster)["violations"] == []

    def test_missing_inference_proxy_autoscaler(self, monkeypatch: pytest.MonkeyPatch) -> None:
        cluster = _healthy_cluster()
        del cluster.objects[
            ("horizontalpodautoscaler", checks.PLATFORM_NAMESPACE, "inference-proxy-hpa")
        ]
        message = self._run(cluster, monkeypatch)
        assert "HorizontalPodAutoscaler inference-proxy-hpa is missing" in message

    def test_autoscaler_pointing_at_the_wrong_deployment(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        cluster = _healthy_cluster()
        cluster.put("horizontalpodautoscaler", _hpa("inference-proxy-hpa", "health-monitor"))
        message = self._run(cluster, monkeypatch)
        assert (
            "HorizontalPodAutoscaler inference-proxy-hpa targets 'health-monitor', "
            "expected 'inference-proxy'"
        ) in message

    def test_manifest_processor_autoscaler_present_while_opted_out(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        cluster = _healthy_cluster()
        cluster.put("horizontalpodautoscaler", _hpa(*checks.MANIFEST_PROCESSOR_AUTOSCALER))
        message = self._run(cluster, monkeypatch)
        assert "manifest-processor-hpa exists although cdk.json leaves" in message

    def test_manifest_processor_autoscaler_missing_while_opted_in(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        cluster = _healthy_cluster()
        message = self._run(
            cluster, monkeypatch, manifest_processor={"autoscaling": {"enabled": True}}
        )
        assert "HorizontalPodAutoscaler manifest-processor-hpa is missing" in message

    def test_missing_enforcement_switch(self, monkeypatch: pytest.MonkeyPatch) -> None:
        cluster = _healthy_cluster()
        del cluster.objects[("configmap", checks.ENFORCEMENT_SWITCH_NAMESPACE, "amazon-vpc-cni")]
        message = self._run(cluster, monkeypatch)
        assert "ConfigMap kube-system/amazon-vpc-cni is missing" in message

    def test_enforcement_switch_must_match_cdk_json(self, monkeypatch: pytest.MonkeyPatch) -> None:
        cluster = _healthy_cluster()
        message = self._run(cluster, monkeypatch, eks_cluster={"network_policy_enforcement": False})
        assert "key enable-network-policy-controller is 'true', expected 'false'" in message
        assert "key enable-network-policy is 'true', expected 'false'" in message

    def test_kubectl_failures_propagate_with_the_recorded_output(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _install_clock(monkeypatch)
        ctx = _context(cdk_context={"cost_monitoring": {"enabled": False}})
        cluster = _healthy_cluster()
        cluster.failures[("deployment", checks.PLATFORM_NAMESPACE, "health-monitor")] = (
            1,
            "",
            "Unable to connect to the server: dial tcp 127.0.0.1:8443: connection refused",
        )
        with pytest.raises(checks_cluster.KubectlError):
            checks.verify_platform_workloads(ctx, REGION, cluster)
        record = ctx.checkpoint.state["platform_workloads"][REGION]
        assert record["last_kubectl_error"]["argv"][:3] == ["get", "deployment", "health-monitor"]


class TestTransientStates:
    """Transient states are polled; the deadline turns them into failures."""

    def test_rollout_in_flight_converges_within_the_deadline(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        clock = _install_clock(monkeypatch)
        ctx = _context(cdk_context={"cost_monitoring": {"enabled": False}})
        cluster = _healthy_cluster()
        rolling = _deployment("manifest-processor", ready=1, available=1, updated=1, observed=2)
        cluster.put("deployment", rolling)
        cluster.pods[(checks.PLATFORM_NAMESPACE, "app=manifest-processor")] = [
            _pod("manifest-processor-a"),
            _pod("manifest-processor-b", ready=False),
        ]
        cluster.put("poddisruptionbudget", _budget("manifest-processor-pdb", allowed=0, healthy=1))
        cluster.put(
            "horizontalpodautoscaler",
            _hpa("inference-proxy-hpa", "inference-proxy", active=False),
        )
        observations = 0

        def progressing(*args: str, **kwargs: Any) -> tuple[int, str, str]:
            # The third look at the rolling Deployment finds everything settled.
            nonlocal observations
            if args[:3] == ("get", "deployment", "manifest-processor"):
                observations += 1
                if observations == 3:
                    cluster.put("deployment", _deployment("manifest-processor"))
                    cluster.pods[(checks.PLATFORM_NAMESPACE, "app=manifest-processor")] = [
                        _pod("manifest-processor-a"),
                        _pod("manifest-processor-b"),
                    ]
                    cluster.put("poddisruptionbudget", _budget("manifest-processor-pdb"))
                    cluster.put(
                        "horizontalpodautoscaler", _hpa("inference-proxy-hpa", "inference-proxy")
                    )
            return cluster(*args, **kwargs)

        snapshot = checks.verify_platform_workloads(ctx, REGION, progressing)

        assert snapshot["pending"] == []
        assert clock.sleeps == [0.0, 0.0]
        record = ctx.checkpoint.state["platform_workloads"][REGION]
        assert record["observations"] == 3
        assert ctx.persist.call_count == 3

    def test_pending_conditions_are_named_at_the_deadline(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        clock = _install_clock(monkeypatch, step=checks._CONVERGENCE_TIMEOUT_SECONDS / 2)
        ctx = _context(cdk_context={"cost_monitoring": {"enabled": False}})
        cluster = _healthy_cluster()
        cluster.put("deployment", _deployment("health-monitor", replicas=3))
        cluster.put("poddisruptionbudget", _budget("inference-monitor-pdb", allowed=0, healthy=1))
        cluster.put(
            "horizontalpodautoscaler",
            _hpa("inference-proxy-hpa", "inference-proxy", able=False),
        )

        with pytest.raises(checks.PlatformWorkloadValidationError) as info:
            checks.verify_platform_workloads(ctx, REGION, cluster)

        message = str(info.value)
        assert f"did not converge within {checks._CONVERGENCE_TIMEOUT_SECONDS}s" in message
        assert (
            "Deployment health-monitor not converged (desired=3 ready=3 available=3 updated=3 "
            "live_pods=2 generation=3/3)"
        ) in message
        assert (
            "PodDisruptionBudget inference-monitor-pdb allows no disruption (healthy=1 desired=0)"
            in (message)
        )
        assert (
            "HorizontalPodAutoscaler inference-proxy-hpa not active (AbleToScale=False "
            "ScalingActive=True)"
        ) in message
        assert len(clock.sleeps) == 2

    @pytest.mark.parametrize(
        "pods",
        [
            [_pod("inference-proxy-a", phase="Pending", ready=False), _pod("inference-proxy-b")],
            [_pod("inference-proxy-a", containers=0), _pod("inference-proxy-b")],
            [_pod("inference-proxy-a")],
        ],
    )
    def test_pods_not_yet_running_and_ready_keep_the_deployment_pending(
        self, monkeypatch: pytest.MonkeyPatch, pods: list[dict[str, Any]]
    ) -> None:
        _install_clock(monkeypatch, step=checks._CONVERGENCE_TIMEOUT_SECONDS)
        ctx = _context(cdk_context={"cost_monitoring": {"enabled": False}})
        cluster = _healthy_cluster()
        cluster.pods[(checks.PLATFORM_NAMESPACE, "app=inference-proxy")] = pods
        with pytest.raises(checks.PlatformWorkloadValidationError, match="inference-proxy not"):
            checks.verify_platform_workloads(ctx, REGION, cluster)


class TestAction:
    def test_visits_every_region_through_its_own_tunnel(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        ctx = _context(
            cdk_context={"manifest_processor": {"autoscaling": {"enabled": True}}},
            regions=("us-east-1", "eu-west-1"),
        )
        opened: list[str] = []
        verified: list[tuple[str, Any]] = []

        @contextlib.contextmanager
        def cluster_kubectl(context: Any, region: str):
            assert context is ctx
            opened.append(region)
            yield f"kubectl-{region}"

        def verify(context: Any, region: str, kubectl: Any) -> dict[str, Any]:
            verified.append((region, kubectl))
            return {"converged": region}

        monkeypatch.setattr(action_module, "cluster_kubectl", cluster_kubectl)
        monkeypatch.setattr(action_module, "verify_platform_workloads", verify)

        evidence = action_module.action_platform_workloads(ctx)

        assert opened == ["us-east-1", "eu-west-1"]
        assert verified == [("us-east-1", "kubectl-us-east-1"), ("eu-west-1", "kubectl-eu-west-1")]
        assert evidence == {
            "deployments": list(ALL_DEPLOYMENTS),
            "manifest_processor_autoscaling": True,
            "network_policy_enforcement": True,
            "regions": {
                "us-east-1": {"converged": "us-east-1"},
                "eu-west-1": {"converged": "eu-west-1"},
            },
        }

    def test_first_failing_region_aborts_the_action(self, monkeypatch: pytest.MonkeyPatch) -> None:
        ctx = _context(regions=("us-east-1", "eu-west-1"))
        opened: list[str] = []

        @contextlib.contextmanager
        def cluster_kubectl(context: Any, region: str):
            opened.append(region)
            yield MagicMock()

        def verify(context: Any, region: str, kubectl: Any) -> dict[str, Any]:
            raise checks.PlatformWorkloadValidationError(f"{region} breached")

        monkeypatch.setattr(action_module, "cluster_kubectl", cluster_kubectl)
        monkeypatch.setattr(action_module, "verify_platform_workloads", verify)

        with pytest.raises(checks.PlatformWorkloadValidationError, match="us-east-1 breached"):
            action_module.action_platform_workloads(ctx)
        assert opened == ["us-east-1"]

    def test_platform_deployments_match_the_applier_inventory(self) -> None:
        """The check and the applier must agree on what a platform Deployment is."""
        spec = importlib.util.spec_from_file_location(
            "kubectl_applier_handler", APPLIER_DIR / "handler.py"
        )
        assert spec is not None and spec.loader is not None
        handler = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(handler)
        applier = [name for _namespace, name, _sa in handler.PLATFORM_DEPLOYMENTS]
        assert applier == list(ALL_DEPLOYMENTS)
        assert {namespace for namespace, _name, _sa in handler.PLATFORM_DEPLOYMENTS} == {
            checks.PLATFORM_NAMESPACE
        }

    def test_expected_objects_exist_in_the_shipped_manifests(self) -> None:
        """Every name the check asks the cluster for is one the applier ships."""
        names: dict[str, set[str]] = {}
        for path in sorted((APPLIER_DIR / "manifests").glob("3*.yaml")):
            text = _PLACEHOLDER.sub("placeholder", path.read_text(encoding="utf-8"))
            for document in yaml.safe_load_all(text):
                if isinstance(document, dict) and "kind" in document:
                    names.setdefault(document["kind"], set()).add(document["metadata"]["name"])
        assert set(ALL_DEPLOYMENTS) <= names["Deployment"]
        assert set(checks.DISRUPTION_BUDGETS) <= names["PodDisruptionBudget"]
        assert {checks.INFERENCE_PROXY_AUTOSCALER[0], checks.MANIFEST_PROCESSOR_AUTOSCALER[0]} <= (
            names["HorizontalPodAutoscaler"]
        )
        switch = yaml.safe_load(
            _PLACEHOLDER.sub(
                "true",
                (APPLIER_DIR / "manifests" / "06-network-policy-controller.yaml").read_text(
                    encoding="utf-8"
                ),
            )
        )
        assert switch["metadata"]["namespace"] == checks.ENFORCEMENT_SWITCH_NAMESPACE
        assert switch["metadata"]["name"] == checks.ENFORCEMENT_SWITCH_NAME
        assert set(switch["data"]) == set(checks.ENFORCEMENT_SWITCH_KEYS)
