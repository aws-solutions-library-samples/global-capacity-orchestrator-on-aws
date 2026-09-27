"""
Tests for the live-validation network-posture action and its probe matrix.

Covers the matrix itself (which client dials which target with which verdict
promised by which shipped policy, including the TLS-sidecar ports that
replaced the plaintext ones and the loopback binds behind them), the Job
lifecycle around every probe (a clean-slate foreground delete, apply from the
checked-in manifest with the run token and PROBE_URL substituted, bounded
waits for a Ready listener or a terminal probe, background delete of every
recorded Job), the stand-in model listener in ``gco-inference``, the
cost-monitor leg proved over the service path (``GET /api/v1/cost/status``),
verdict classification from phase plus exit code, disruptions (a probe whose
pod was evicted is read at once and re-run once from a fresh Job, a second
disruption is named as one, a verdict exit code stands on a pod marked for
disruption, and a listener, inference-monitor, or cost-monitor pod that did
not last the matrix voids its verdicts), the enforcement-off and
cost-monitoring-off skips, the fail-closed handling of listener failures,
missing platform pods, kubectl failures, deadlines, and cleanup problems, the
checkpoint record that authorizes cleanup, and the action's per-Region tunnel
session. Every kubectl, tunnel, API, and clock boundary is faked; the
manifests are the real files.
"""

from __future__ import annotations

import contextlib
import json
import re
import threading
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pytest
import yaml

from cli.jobs import JobManager
from scripts.live_release_validation.actions import network_posture as action_module
from scripts.live_release_validation.checks import jobs as checks_jobs
from scripts.live_release_validation.checks import network_posture as checks
from scripts.live_release_validation.checks.cluster import KubectlError

REGION = "us-east-1"
_MANIFESTS = Path(__file__).resolve().parents[1] / "lambda" / "kubectl-applier-simple" / "manifests"
#: Manifest placeholders parse as YAML flow mappings; neutralize them before loading.
_PLACEHOLDER = re.compile(r"\{\{[A-Z0-9_]+\}\}")
TOKEN = checks_jobs._run_token("run-123")
TARGET_JOB = f"gco-live-netpol-target-{TOKEN}"
MODEL_JOB = f"gco-live-netpol-model-{TOKEN}"
TARGET_IPS = {"gco-system": "10.0.1.5", "gco-jobs": "10.0.2.7", "gco-inference": "10.0.4.2"}
LISTENER_JOBS = {"gco-system": TARGET_JOB, "gco-jobs": TARGET_JOB, "gco-inference": MODEL_JOB}
MONITOR_IP = "10.0.3.9"
COST_IP = "10.0.5.3"
PROBE_NAMES = (
    "same-namespace",
    "cross-jobs",
    "cross-system",
    "metrics-open",
    "metrics-plaintext",
    "cost-from-default",
    "cost-plaintext",
    "model-from-default",
    "model-from-jobs",
    "https-egress",
    "http-egress",
    "pod-identity-agent",
)
API_PROBE = "cost-via-manifest-processor"
#: Deny verdicts promised only while the policy controller is on.
DENY_PROBES = (
    "cross-jobs",
    "cross-system",
    "cost-from-default",
    "model-from-default",
    "model-from-jobs",
    "http-egress",
)
#: Loopback binds: blocked whatever the policy controller does.
LOOPBACK_PROBES = ("metrics-plaintext", "cost-plaintext")
COST_PROBES = ("cost-from-default", "cost-plaintext")
#: What a correctly enforcing cluster answers, per probe.
ENFORCED = {
    "same-namespace": ("Succeeded", 0),
    "cross-jobs": ("Failed", 42),
    "cross-system": ("Failed", 42),
    "metrics-open": ("Succeeded", 0),
    "metrics-plaintext": ("Failed", 42),
    "cost-from-default": ("Failed", 42),
    "cost-plaintext": ("Failed", 42),
    "model-from-default": ("Failed", 42),
    "model-from-jobs": ("Failed", 42),
    "https-egress": ("Succeeded", 0),
    "http-egress": ("Failed", 42),
    "pod-identity-agent": ("Succeeded", 0),
}
#: Probes that dial only through the policy under test, whatever the answer.
NOT_BLOCKED_WHEN_OPEN = {**ENFORCED, **dict.fromkeys(DENY_PROBES, ("Succeeded", 0))}


class _Clock:
    def __init__(self, start: float = 1_000.0, *, step: float = 1.0) -> None:
        self.now = start
        self.step = step
        self.sleeps: list[float] = []

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(float(seconds))
        self.now += max(float(seconds), self.step)


def _install_clock(monkeypatch: pytest.MonkeyPatch, **kwargs: Any) -> _Clock:
    clock = _Clock(**kwargs)
    monkeypatch.setattr(checks, "time", clock)
    return clock


def _context(
    *,
    cdk_context: dict[str, Any] | None = None,
    regions: tuple[str, ...] = (REGION,),
    state: dict[str, Any] | None = None,
    cost_status: int = 200,
) -> SimpleNamespace:
    settings = SimpleNamespace(
        run_id="run-123",
        poll_interval_seconds=0,
        command_timeout_seconds=30,
        repo_root=Path("/repo"),
        report_dir=Path("/private"),
        kubeconfig_path=Path("/private/kubeconfig"),
    )
    session = MagicMock()
    session.get_partition_for_region.return_value = "aws"
    aws_client = MagicMock()
    aws_client.make_authenticated_request.return_value = SimpleNamespace(status_code=cost_status)
    return SimpleNamespace(
        settings=settings,
        checkpoint=SimpleNamespace(state={} if state is None else state),
        state_lock=threading.RLock(),
        deployment_regions=regions,
        config=SimpleNamespace(project_name="gco-live", global_region="us-east-1"),
        cdk_context={} if cdk_context is None else cdk_context,
        session=session,
        aws_client=aws_client,
        persist=MagicMock(),
        # __init__ builds AWS clients; load_manifests reads no constructor state.
        job_manager=JobManager.__new__(JobManager),
    )


def _pod(
    name: str,
    *,
    phase: str = "Running",
    ready: bool = True,
    ip: str | None = "10.0.0.1",
    exit_code: int | None = None,
    deleting: bool = False,
    containers: bool = True,
    conditions: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    metadata: dict[str, Any] = {"name": name}
    if deleting:
        metadata["deletionTimestamp"] = "2026-09-10T00:00:00Z"
    status: dict[str, Any] = {"phase": phase}
    if ip is not None:
        status["podIP"] = ip
    if conditions is not None:
        status["conditions"] = conditions
    if containers:
        container: dict[str, Any] = {"name": "c", "ready": ready}
        if exit_code is not None:
            container["state"] = {"terminated": {"exitCode": exit_code}}
        status["containerStatuses"] = [container]
    return {"metadata": metadata, "status": status}


def _probe_name(job_name: str) -> str:
    return job_name.removeprefix("gco-live-netpol-").removesuffix(f"-{TOKEN}")


def _evicted(reason: str = "EvictionByEvictionAPI") -> list[dict[str, Any]]:
    """The conditions an eviction leaves on a pod: not Ready, and a DisruptionTarget."""
    return [
        {"type": "Ready", "status": "False"},
        {
            "type": "DisruptionTarget",
            "status": "True",
            "reason": reason,
            "message": "Eviction API: evicting",
        },
    ]


#: The Job conditions a backoffLimit-0 Job carries once its only pod is gone.
_JOB_FAILED = [
    {"type": "FailureTarget", "status": "True", "reason": "BackoffLimitExceeded"},
    {
        "type": "Failed",
        "status": "True",
        "reason": "BackoffLimitExceeded",
        "message": "Job has reached the specified backoff limit",
    },
]


class _FakeCluster:
    """Scripted kubectl for the probe matrix.

    ``apply`` materializes the Job's pod frames: listeners come up Ready with
    the namespace's target IP, probes terminate with the scripted verdict.
    Each ``get pods`` for a Job returns the next frame until the last one. A
    probe Job applied a second time (a re-run) gets its ``rerun_frames`` and a
    ``-rerun`` pod, and loses the ``job_conditions`` that ``get job`` answers.
    """

    def __init__(self, verdicts: dict[str, tuple[str, int | None]] | None = None) -> None:
        self.verdicts: dict[str, tuple[str, int | None]] = dict(
            ENFORCED if verdicts is None else verdicts
        )
        self.frames: dict[tuple[str, str], list[list[dict[str, Any]]]] = {}
        self.listener_frames: dict[str, list[list[dict[str, Any]]]] = {}
        self.probe_frames: dict[str, list[list[dict[str, Any]]]] = {}
        self.monitor_items: list[dict[str, Any]] = [_pod("inference-monitor-x", ip=MONITOR_IP)]
        #: Inference-monitor reads answered in order before ``monitor_items``.
        self.monitor_frames: list[list[dict[str, Any]]] = []
        self.cost_items: list[dict[str, Any]] = [_pod("cost-monitor-x", ip=COST_IP)]
        #: Cost-monitor reads answered in order before ``cost_items``.
        self.cost_frames: list[list[dict[str, Any]]] = []
        self.logs: dict[str, tuple[str, str]] = {}
        #: Job status conditions, per probe name, for ``get job``.
        self.job_conditions: dict[str, list[dict[str, Any]]] = {}
        #: Pod frames a re-created probe Job gets instead of its first ones.
        self.rerun_frames: dict[str, list[list[dict[str, Any]]]] = {}
        self.delete_failures: set[str] = set()
        self.clear_failures: set[str] = set()
        self.apply_failures: set[str] = set()
        self.applied: list[dict[str, Any]] = []
        self.calls: list[tuple[str, ...]] = []
        self.deleted: list[tuple[str, str, str]] = []

    def __call__(self, *args: str, timeout: float, **kwargs: Any) -> tuple[int, str, str]:
        assert timeout == 30.0
        self.calls.append(args)
        verb = args[0]
        if verb == "delete":
            return self._delete(args)
        if verb == "apply":
            return self._apply(args, kwargs["input"])
        if verb == "logs":
            stdout, stderr = self.logs[args[1]]
            return 0, stdout, stderr
        if verb == "get" and args[1] == "job":
            assert args[-2:] == ("--output", "json")
            conditions = self.job_conditions.get(_probe_name(args[2]), [])
            return 0, json.dumps({"status": {"conditions": conditions}}), ""
        assert verb == "get" and args[1] == "pods" and args[-2:] == ("--output", "json")
        namespace = args[args.index("--namespace") + 1]
        selector = args[args.index("--selector") + 1]
        if selector == "app=inference-monitor":
            items = self.monitor_frames.pop(0) if self.monitor_frames else self.monitor_items
            return 0, json.dumps({"items": items}), ""
        if selector == "app=cost-monitor":
            items = self.cost_frames.pop(0) if self.cost_frames else self.cost_items
            return 0, json.dumps({"items": items}), ""
        frames = self.frames[(namespace, selector.removeprefix("job-name="))]
        items = frames.pop(0) if len(frames) > 1 else frames[0]
        return 0, json.dumps({"items": items}), ""

    def _delete(self, args: tuple[str, ...]) -> tuple[int, str, str]:
        name = args[2]
        namespace = args[args.index("--namespace") + 1]
        mode = "clear" if "--cascade=foreground" in args else "cleanup"
        self.deleted.append((mode, namespace, name))
        failures = self.clear_failures if mode == "clear" else self.delete_failures
        if name in failures:
            return 1, "", f"error deleting {namespace}/{name}"
        return 0, f'job.batch "{name}" deleted', ""

    def _apply(self, args: tuple[str, ...], payload: str) -> tuple[int, str, str]:
        job = json.loads(payload)
        namespace = job["metadata"]["namespace"]
        name = job["metadata"]["name"]
        assert args[args.index("--namespace") + 1] == namespace
        self.applied.append(job)
        if name in self.apply_failures:
            return 1, "", f"error creating {namespace}/{name}"
        if name in (TARGET_JOB, MODEL_JOB):
            frames = self.listener_frames.get(
                namespace, [[_pod(f"{name}-pod", ip=TARGET_IPS[namespace])]]
            )
        else:
            probe = _probe_name(name)
            phase, exit_code = self.verdicts[probe]
            rerun = (namespace, name) in self.frames
            if rerun:
                # A re-created Job starts clean: a pod of its own, no Failed condition.
                self.job_conditions.pop(probe, None)
            pod = f"{name}-rerun" if rerun else f"{name}-pod"
            scripted = self.rerun_frames if rerun else self.probe_frames
            frames = scripted.get(probe, [[_pod(pod, phase=phase, exit_code=exit_code)]])
            verdict = "REACHABLE" if phase == "Succeeded" else "BLOCKED"
            self.logs.setdefault(pod, (f"NETPOL_{verdict}\n", ""))
        self.frames[(namespace, name)] = [list(frame) for frame in frames]
        return 0, f"job.batch/{name} created", ""


def _run(
    ctx: SimpleNamespace,
    cluster: _FakeCluster,
    monkeypatch: pytest.MonkeyPatch,
    **clock: Any,
) -> dict[str, Any]:
    _install_clock(monkeypatch, **clock)
    return checks.NetworkPostureProbe(ctx, REGION, cluster).run()


def _failure(
    ctx: SimpleNamespace,
    cluster: _FakeCluster,
    monkeypatch: pytest.MonkeyPatch,
    match: str,
    **clock: Any,
) -> None:
    _install_clock(monkeypatch, **clock)
    with pytest.raises(checks.NetworkPostureValidationError, match=match):
        checks.NetworkPostureProbe(ctx, REGION, cluster).run()


def _listeners() -> list[tuple[str, str]]:
    return list(LISTENER_JOBS.items())


class TestProbeMatrix:
    def test_every_promise_of_the_shipped_policies_has_a_probe(self) -> None:
        specs = checks._probe_specs(
            "1.1.1.1", "2.2.2.2", "3.3.3.3", model_target_ip="4.4.4.4", cost_monitor_ip="5.5.5.5"
        )
        assert [spec.name for spec in specs] == list(PROBE_NAMES)
        by_name = {spec.name: spec for spec in specs}
        assert by_name["same-namespace"].url == "http://2.2.2.2:8080/"
        assert by_name["cross-jobs"].client_namespace == "default"
        assert by_name["cross-system"].url == "http://1.1.1.1:8080/"
        # The metrics are served by the TLS sidecar; the plaintext port is loopback-only.
        assert by_name["metrics-open"].url == "https://3.3.3.3:9443/metrics"
        assert by_name["metrics-plaintext"].url == "http://3.3.3.3:9090/metrics"
        assert by_name["cost-from-default"].url == "https://5.5.5.5:8443/"
        assert by_name["cost-plaintext"].url == "http://5.5.5.5:8080/"
        assert by_name["model-from-default"].url == "http://4.4.4.4:8443/"
        assert by_name["model-from-jobs"].client_namespace == "gco-jobs"
        assert by_name["https-egress"].url == "https://checkip.amazonaws.com/"
        assert by_name["http-egress"].url == "http://checkip.amazonaws.com/"
        assert by_name["pod-identity-agent"].url == "http://169.254.170.23/v1/credentials"
        assert by_name["pod-identity-agent"].client_namespace == "gco-jobs"
        assert {spec.name for spec in specs if spec.expected == "blocked"} == {
            *DENY_PROBES,
            *LOOPBACK_PROBES,
        }
        assert {spec.name for spec in specs if spec.enforcement_only} == set(DENY_PROBES)
        assert {spec.name for spec in specs if spec.requires_cost_monitor} == set(COST_PROBES)
        for spec in specs:
            assert spec.expected in ("reachable", "blocked")
            assert len(f"gco-live-netpol-{spec.name}-{TOKEN}") <= 63
            assert spec.rule

    def test_cost_probes_carry_a_placeholder_host_without_a_cost_monitor(self) -> None:
        specs = checks._probe_specs(
            "1.1.1.1", "2.2.2.2", "3.3.3.3", model_target_ip="4.4.4.4", cost_monitor_ip=None
        )
        by_name = {spec.name: spec for spec in specs}
        assert by_name["cost-from-default"].url == "https://cost-monitor-not-deployed:8443/"
        assert by_name["cost-plaintext"].url == "http://cost-monitor-not-deployed:8080/"

    def test_probe_names_are_kubernetes_safe(self) -> None:
        for name in PROBE_NAMES:
            assert name == name.lower() and set(name) <= set("abcdefghijklmnopqrstuvwxyz-")

    def test_the_service_path_leg_names_the_policies_it_proves(self) -> None:
        spec = checks._COST_VIA_MANIFEST_PROCESSOR
        assert spec.name == API_PROBE
        assert spec.expected == "reachable"
        assert spec.requires_cost_monitor is True
        assert spec.enforcement_only is False
        assert "/api/v1/cost/status" in spec.url
        assert "8443" in spec.rule


class TestEnforcedCluster:
    def test_matrix_passes_and_leaves_nothing_behind(self, monkeypatch: pytest.MonkeyPatch) -> None:
        ctx = _context()
        cluster = _FakeCluster()

        evidence = _run(ctx, cluster, monkeypatch)

        assert evidence["enforcement_configured"] is True
        assert evidence["cost_monitoring_configured"] is True
        assert evidence["targets"] == {
            "gco-system": {"job": TARGET_JOB, "pod": f"{TARGET_JOB}-pod", "ip": "10.0.1.5"},
            "gco-jobs": {"job": TARGET_JOB, "pod": f"{TARGET_JOB}-pod", "ip": "10.0.2.7"},
            "gco-inference": {"job": MODEL_JOB, "pod": f"{MODEL_JOB}-pod", "ip": "10.0.4.2"},
        }
        assert evidence["inference_monitor"] == {"pod": "inference-monitor-x", "ip": MONITOR_IP}
        assert evidence["cost_monitor"] == {"pod": "cost-monitor-x", "ip": COST_IP}
        assert [probe["name"] for probe in evidence["probes"]] == [*PROBE_NAMES, API_PROBE]
        assert {probe["status"] for probe in evidence["probes"]} == {"matched"}
        assert evidence["cleanup_problems"] == []
        cross_jobs = next(probe for probe in evidence["probes"] if probe["name"] == "cross-jobs")
        assert cross_jobs["observed"] == "blocked"
        assert cross_jobs["exit_code"] == 42
        assert cross_jobs["job"] == f"gco-live-netpol-cross-jobs-{TOKEN}"
        assert cross_jobs["output"] == "NETPOL_BLOCKED\n"
        # A log without the sampling trace still yields the evidence shape.
        assert cross_jobs["samples"] == []
        assert cross_jobs["settled"] is None
        assert cross_jobs["attach_window_observed"] is False
        # The manifest processor's leg rides the service path through the API.
        api = evidence["probes"][-1]
        assert api["via"] == "api"
        assert api["status_code"] == 200
        assert api["observed"] == "reachable"
        ctx.aws_client.make_authenticated_request.assert_called_once_with(
            method="GET", path="/api/v1/cost/status", target_region=None
        )

        # Three listeners then twelve probes, each cleared before creation and
        # deleted afterwards; the record holds every Job before it exists.
        created = [
            (job["metadata"]["namespace"], job["metadata"]["name"]) for job in cluster.applied
        ]
        assert created[:3] == _listeners()
        assert created[3:] == [
            ("gco-jobs", f"gco-live-netpol-same-namespace-{TOKEN}"),
            ("default", f"gco-live-netpol-cross-jobs-{TOKEN}"),
            ("gco-jobs", f"gco-live-netpol-cross-system-{TOKEN}"),
            ("default", f"gco-live-netpol-metrics-open-{TOKEN}"),
            ("default", f"gco-live-netpol-metrics-plaintext-{TOKEN}"),
            ("default", f"gco-live-netpol-cost-from-default-{TOKEN}"),
            ("default", f"gco-live-netpol-cost-plaintext-{TOKEN}"),
            ("default", f"gco-live-netpol-model-from-default-{TOKEN}"),
            ("gco-jobs", f"gco-live-netpol-model-from-jobs-{TOKEN}"),
            ("gco-jobs", f"gco-live-netpol-https-egress-{TOKEN}"),
            ("gco-jobs", f"gco-live-netpol-http-egress-{TOKEN}"),
            ("gco-jobs", f"gco-live-netpol-pod-identity-agent-{TOKEN}"),
        ]
        assert [entry[1:] for entry in cluster.deleted if entry[0] == "clear"] == created
        assert [entry[1:] for entry in cluster.deleted if entry[0] == "cleanup"] == created
        record = ctx.checkpoint.state["network_posture"][REGION]
        assert [(job["namespace"], job["name"]) for job in record["jobs"]] == created
        assert all(job["deleted"] for job in record["jobs"])
        assert record["evidence"] is evidence
        assert record["targets"] == evidence["targets"]
        assert record["probes"] is evidence["probes"]
        assert "failure" not in record
        assert ctx.persist.called

    def test_jobs_come_from_the_checked_in_manifests_with_the_run_identity(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        ctx = _context()
        cluster = _FakeCluster()

        _run(ctx, cluster, monkeypatch)

        listener = cluster.applied[0]
        assert listener["kind"] == "Job"
        assert listener["metadata"]["labels"]["gco.aws/validation-run"] == TOKEN
        assert listener["metadata"]["labels"]["gco.aws/validation-path"] == "network-posture"
        pod_labels = listener["spec"]["template"]["metadata"]["labels"]
        assert pod_labels["gco.aws/validation-run"] == TOKEN
        assert "gco.io/type" not in pod_labels
        container = listener["spec"]["template"]["spec"]["containers"][0]
        assert container["image"].startswith("docker.io/library/busybox:1.38.0@sha256:")
        assert "httpd -f -p 8080" in container["command"][-1]
        assert container["readinessProbe"]["exec"]["command"][-1] == "http://127.0.0.1:8080/"
        assert "env" not in container
        assert listener["spec"]["activeDeadlineSeconds"] == 1800
        assert listener["spec"]["ttlSecondsAfterFinished"] == 600

        # The model stand-in carries the label the model ingress policies
        # select, and nothing the inference monitor would adopt it by.
        model = cluster.applied[2]
        assert model["metadata"]["namespace"] == "gco-inference"
        assert model["metadata"]["name"] == MODEL_JOB
        model_labels = model["spec"]["template"]["metadata"]["labels"]
        assert model_labels["gco.io/type"] == "inference"
        assert model_labels["gco.aws/validation-run"] == TOKEN
        assert "project" not in model_labels
        assert "app" not in model_labels
        model_container = model["spec"]["template"]["spec"]["containers"][0]
        assert model_container["image"] == container["image"]
        assert "httpd -f -p 8443" in model_container["command"][-1]
        assert model_container["ports"] == [
            {"name": "model", "containerPort": 8443, "protocol": "TCP"}
        ]
        assert model_container["readinessProbe"]["exec"]["command"][-1] == (
            "http://127.0.0.1:8443/"
        )
        assert model["spec"]["template"]["spec"]["automountServiceAccountToken"] is False
        assert model["spec"]["activeDeadlineSeconds"] == 1800

        probe = cluster.applied[3]
        env = {
            entry["name"]: entry["value"]
            for entry in probe["spec"]["template"]["spec"]["containers"][0]["env"]
        }
        assert env == {
            "PROBE_URL": "http://10.0.2.7:8080/",
            # The VPC CNI admits a new pod's traffic until its policies attach,
            # so a verdict is a steady state: held for 30s, read no earlier than
            # 45s in, given up on (exit 43) after three minutes of flapping.
            "PROBE_SETTLE_SECONDS": "30",
            "PROBE_MIN_OBSERVATION_SECONDS": "45",
            "PROBE_BUDGET_SECONDS": "180",
        }
        script = probe["spec"]["template"]["spec"]["containers"][0]["command"][-1]
        assert 'timeout 15 wget -O /dev/null -T 5 "$PROBE_URL"' in script
        assert "grep -q 'HTTP/'" in script
        assert 'echo "NETPOL_SAMPLE t=$((now - start))s $verdict"' in script
        assert '-ge "$PROBE_SETTLE_SECONDS"' in script
        assert '-ge "$PROBE_MIN_OBSERVATION_SECONDS"' in script
        assert '-ge "$PROBE_BUDGET_SECONDS"' in script
        assert 'echo "NETPOL_UNSETTLED samples=$samples"\n    exit 43' in script
        assert "NETPOL_SETTLED verdict=$current" in script
        assert "echo NETPOL_REACHABLE\n  exit 0" in script
        assert "echo NETPOL_BLOCKED\nexit 42" in script
        assert probe["spec"]["template"]["spec"]["securityContext"]["runAsNonRoot"] is True
        assert probe["spec"]["activeDeadlineSeconds"] == 600
        assert "__PROBE_URL__" not in json.dumps(probe)
        assert "__RUN_TOKEN__" not in json.dumps(cluster.applied)
        metrics = next(
            job
            for job in cluster.applied
            if job["metadata"]["name"] == f"gco-live-netpol-metrics-open-{TOKEN}"
        )
        metrics_env = metrics["spec"]["template"]["spec"]["containers"][0]["env"]
        assert metrics_env[0] == {
            "name": "PROBE_URL",
            "value": f"https://{MONITOR_IP}:9443/metrics",
        }

    def test_slow_listeners_and_probes_are_polled_to_completion(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        ctx = _context()
        cluster = _FakeCluster()
        cluster.listener_frames["gco-jobs"] = [
            [],
            [_pod("old", deleting=True), _pod(f"{TARGET_JOB}-pod", phase="Pending", ready=False)],
            [_pod(f"{TARGET_JOB}-pod", ready=False, ip="10.0.2.7")],
            [_pod(f"{TARGET_JOB}-pod", ip=None)],
            [_pod(f"{TARGET_JOB}-pod", containers=False, ip="10.0.2.7")],
            [_pod(f"{TARGET_JOB}-pod", ip="10.0.2.7")],
        ]
        probe = f"gco-live-netpol-https-egress-{TOKEN}-pod"
        cluster.probe_frames["https-egress"] = [
            [],
            [_pod(probe, phase="Pending", ready=False)],
            [_pod(probe, phase="Succeeded", exit_code=0)],
        ]
        cluster.logs[probe] = ("", "NETPOL_REACHABLE (from stderr)")
        clock = _install_clock(monkeypatch)

        evidence = checks.NetworkPostureProbe(ctx, REGION, cluster).run()

        assert evidence["targets"]["gco-jobs"]["ip"] == "10.0.2.7"
        https = next(item for item in evidence["probes"] if item["name"] == "https-egress")
        assert https["status"] == "matched"
        assert https["output"] == "NETPOL_REACHABLE (from stderr)"
        assert len(clock.sleeps) == 7

    def test_a_verdict_is_the_steady_state_and_the_attach_window_is_evidence(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The fourth live run's http-egress probe, had it sampled.

        The VPC CNI admitted the pod's first dial before its egress policies
        were attached; the steady state is the verdict, and the early answer
        is kept as evidence rather than read as a mismatch.
        """
        ctx = _context()
        cluster = _FakeCluster()
        probe = f"gco-live-netpol-http-egress-{TOKEN}-pod"
        cluster.logs[probe] = (
            "NETPOL_SAMPLE t=0s reachable\n"
            "NETPOL_SAMPLE t=3s blocked\n"
            "NETPOL_SETTLED verdict=blocked held=42s samples=7\n"
            "Connecting to checkip.amazonaws.com (18.0.0.1:80)\n"
            "wget: download timed out\n"
            "NETPOL_BLOCKED\n",
            "",
        )
        steady = f"gco-live-netpol-https-egress-{TOKEN}-pod"
        cluster.logs[steady] = (
            "NETPOL_SAMPLE t=0s reachable\n"
            "NETPOL_SETTLED verdict=reachable held=45s samples=21\n"
            "NETPOL_REACHABLE\n",
            "",
        )

        evidence = _run(ctx, cluster, monkeypatch)

        by_name = {item["name"]: item for item in evidence["probes"]}
        assert by_name["http-egress"]["status"] == "matched"
        assert by_name["http-egress"]["observed"] == "blocked"
        assert by_name["http-egress"]["samples"] == ["t=0s reachable", "t=3s blocked"]
        assert (
            by_name["http-egress"]["settled"] == "NETPOL_SETTLED verdict=blocked held=42s samples=7"
        )
        assert by_name["http-egress"]["attach_window_observed"] is True
        assert by_name["https-egress"]["samples"] == ["t=0s reachable"]
        assert by_name["https-egress"]["settled"] == (
            "NETPOL_SETTLED verdict=reachable held=45s samples=21"
        )
        assert by_name["https-egress"]["attach_window_observed"] is False
        # The tail asked for is long enough to keep a flapping trace's end.
        logs_call = next(call for call in cluster.calls if call[0] == "logs" and call[1] == probe)
        assert "--tail=40" in logs_call

    def test_an_unsettled_probe_is_named_with_its_trace(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        ctx = _context()
        verdicts: dict[str, tuple[str, int | None]] = dict(ENFORCED)
        verdicts["http-egress"] = ("Failed", 43)
        cluster = _FakeCluster(verdicts)
        probe = f"gco-live-netpol-http-egress-{TOKEN}-pod"
        cluster.logs[probe] = (
            "NETPOL_SAMPLE t=0s reachable\n"
            "NETPOL_SAMPLE t=9s blocked\n"
            "NETPOL_SAMPLE t=30s reachable\n"
            "NETPOL_UNSETTLED samples=40\n",
            "",
        )

        _failure(
            ctx,
            cluster,
            monkeypatch,
            match=r"http-egress .* expected blocked, observed unsettled \[phase=Failed exit=43\]",
        )

        probes = ctx.checkpoint.state["network_posture"][REGION]["probes"]
        unsettled = next(item for item in probes if item["name"] == "http-egress")
        assert unsettled["status"] == "mismatch"
        assert unsettled["samples"] == ["t=0s reachable", "t=9s blocked", "t=30s reachable"]
        assert unsettled["settled"] == "NETPOL_UNSETTLED samples=40"
        assert unsettled["attach_window_observed"] is True

    def test_an_existing_checkpoint_record_is_reused(self, monkeypatch: pytest.MonkeyPatch) -> None:
        stale_job = {"namespace": "gco-jobs", "name": "left-over", "deleted": False}
        gone_job = {"namespace": "default", "name": "already-gone", "deleted": True}
        state = {"network_posture": {REGION: {"jobs": [stale_job, gone_job], "note": "kept"}}}
        ctx = _context(state=state)
        cluster = _FakeCluster()

        _run(ctx, cluster, monkeypatch)

        record = ctx.checkpoint.state["network_posture"][REGION]
        assert record["note"] == "kept"
        assert record["jobs"][0] is stale_job
        assert stale_job["deleted"] is True
        assert ("cleanup", "gco-jobs", "left-over") in cluster.deleted
        assert ("cleanup", "default", "already-gone") not in cluster.deleted


class TestEnforcementDisabled:
    def test_deny_probes_are_skipped_with_the_configuration_source(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        ctx = _context(cdk_context={"eks_cluster": {"network_policy_enforcement": False}})
        # Nothing is denied any more, but a loopback bind is not a policy.
        cluster = _FakeCluster(dict(NOT_BLOCKED_WHEN_OPEN))

        evidence = _run(ctx, cluster, monkeypatch)

        assert evidence["enforcement_configured"] is False
        statuses = {probe["name"]: probe["status"] for probe in evidence["probes"]}
        assert statuses == {
            **dict.fromkeys(DENY_PROBES, "skipped"),
            "same-namespace": "matched",
            "metrics-open": "matched",
            "metrics-plaintext": "matched",
            "cost-plaintext": "matched",
            "https-egress": "matched",
            "pod-identity-agent": "matched",
            API_PROBE: "matched",
        }
        skipped = next(probe for probe in evidence["probes"] if probe["name"] == "cross-jobs")
        assert "network_policy_enforcement is false" in skipped["reason"]
        assert "observed" not in skipped
        launched = {_probe_name(job["metadata"]["name"]) for job in cluster.applied[3:]}
        assert launched == {
            "same-namespace",
            "metrics-open",
            "metrics-plaintext",
            "cost-plaintext",
            "https-egress",
            "pod-identity-agent",
        }


class TestCostMonitoringDisabled:
    @pytest.mark.parametrize(
        "cdk_context",
        [{"cost_monitoring": {"enabled": False}}, {"cluster_observability": {"enabled": False}}],
    )
    def test_cost_probes_and_the_service_path_leg_are_skipped(
        self, monkeypatch: pytest.MonkeyPatch, cdk_context: dict[str, Any]
    ) -> None:
        ctx = _context(cdk_context=cdk_context)
        cluster = _FakeCluster()

        evidence = _run(ctx, cluster, monkeypatch)

        assert evidence["cost_monitoring_configured"] is False
        assert "cost_monitor" not in evidence
        by_name = {probe["name"]: probe for probe in evidence["probes"]}
        for name in (*COST_PROBES, API_PROBE):
            assert by_name[name]["status"] == "skipped"
            assert "cost monitoring is disabled" in by_name[name]["reason"]
        assert {
            probe["status"]
            for name, probe in by_name.items()
            if name not in (*COST_PROBES, API_PROBE)
        } == {"matched"}
        assert not any("app=cost-monitor" in call for call in cluster.calls)
        ctx.aws_client.make_authenticated_request.assert_not_called()
        launched = {_probe_name(job["metadata"]["name"]) for job in cluster.applied[3:]}
        assert launched.isdisjoint(COST_PROBES)


class TestServicePathLeg:
    def test_a_failing_cost_status_is_a_named_mismatch(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        ctx = _context(cost_status=503)
        cluster = _FakeCluster()

        _failure(
            ctx,
            cluster,
            monkeypatch,
            match=(
                r"cost-via-manifest-processor \(gco-system -> GET /api/v1/cost/status -> "
                r"https://cost-monitor\.gco-system\.svc\.cluster\.local:8443\) expected "
                r"reachable, observed error \[HTTP 503\]$"
            ),
        )

        record = ctx.checkpoint.state["network_posture"][REGION]
        api = record["probes"][-1]
        assert api["name"] == API_PROBE
        assert api["status"] == "mismatch"
        assert api["status_code"] == 503
        assert all(job["deleted"] for job in record["jobs"])

    def test_no_region_attributable_transport_skips_the_leg(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Two Regions without the regional API: the global endpoint cannot be
        # pinned to this cluster, so the service-path leg is not attributable.
        ctx = _context(regions=(REGION, "eu-west-1"))
        cluster = _FakeCluster()

        evidence = _run(ctx, cluster, monkeypatch)

        api = evidence["probes"][-1]
        assert api["name"] == API_PROBE
        assert api["status"] == "skipped"
        assert "regional_api_enabled" in api["reason"]
        ctx.aws_client.make_authenticated_request.assert_not_called()

    def test_the_regional_api_pins_the_leg_to_this_region(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        ctx = _context(
            cdk_context={"api_gateway": {"regional_api_enabled": True}},
            regions=(REGION, "eu-west-1"),
        )
        cluster = _FakeCluster()

        evidence = _run(ctx, cluster, monkeypatch)

        assert evidence["probes"][-1]["status"] == "matched"
        ctx.aws_client.make_authenticated_request.assert_called_once_with(
            method="GET", path="/api/v1/cost/status", target_region=REGION
        )


class TestVerdictMismatches:
    def test_an_open_cluster_fails_with_every_mismatch_named(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        ctx = _context()
        cluster = _FakeCluster(dict.fromkeys(PROBE_NAMES, ("Succeeded", 0)))

        _failure(ctx, cluster, monkeypatch, match="network posture in us-east-1")

        record = ctx.checkpoint.state["network_posture"][REGION]
        failure = record["failure"]
        assert (
            "cross-jobs (default -> http://10.0.2.7:8080/) expected blocked, observed reachable "
            "[phase=Succeeded exit=0]"
        ) in failure
        assert "cross-system (gco-jobs -> http://10.0.1.5:8080/)" in failure
        assert "http-egress (gco-jobs -> http://checkip.amazonaws.com/)" in failure
        assert f"metrics-plaintext (default -> http://{MONITOR_IP}:9090/metrics)" in failure
        assert f"cost-from-default (default -> https://{COST_IP}:8443/)" in failure
        assert f"cost-plaintext (default -> http://{COST_IP}:8080/)" in failure
        assert "model-from-default (default -> http://10.0.4.2:8443/)" in failure
        assert "model-from-jobs (gco-jobs -> http://10.0.4.2:8443/)" in failure
        assert "same-namespace" not in failure
        assert API_PROBE not in failure
        # Everything the run created was still deleted.
        assert all(job["deleted"] for job in record["jobs"])
        assert len(record["jobs"]) == 15
        assert "evidence" not in record

    @pytest.mark.parametrize(
        ("verdict", "observed"),
        [
            (("Failed", 1), "error"),
            (("Failed", 0), "error"),
            (("Succeeded", 42), "error"),
            (("Succeeded", 43), "error"),
            (("Failed", None), "error"),
            (("Failed", 42), "blocked"),
            # The answer flapped for the probe's whole budget: named, never a verdict.
            (("Failed", 43), "unsettled"),
        ],
    )
    def test_anything_but_the_two_verdict_codes_is_a_probe_error(
        self, monkeypatch: pytest.MonkeyPatch, verdict: tuple[str, int | None], observed: str
    ) -> None:
        ctx = _context()
        verdicts: dict[str, tuple[str, int | None]] = dict(ENFORCED)
        verdicts["same-namespace"] = verdict
        cluster = _FakeCluster(verdicts)
        if verdict[1] is None:
            cluster.probe_frames["same-namespace"] = [
                [
                    _pod(
                        f"gco-live-netpol-same-namespace-{TOKEN}-pod",
                        phase="Failed",
                        containers=False,
                    )
                ]
            ]

        _failure(ctx, cluster, monkeypatch, match=f"expected reachable, observed {observed}")

        probes = ctx.checkpoint.state["network_posture"][REGION]["probes"]
        assert probes[0]["observed"] == observed
        assert probes[0]["status"] == "mismatch"
        # No DisruptionTarget on the pod, so the probe itself broke: it is read
        # once and never re-run.
        assert probes[0]["disruptions"] == []
        applied = [job["metadata"]["name"] for job in cluster.applied]
        assert applied.count(f"gco-live-netpol-same-namespace-{TOKEN}") == 1


class TestFailClosed:
    def test_listener_that_fails_before_serving(self, monkeypatch: pytest.MonkeyPatch) -> None:
        ctx = _context()
        cluster = _FakeCluster()
        cluster.listener_frames["gco-system"] = [
            [_pod(f"{TARGET_JOB}-pod", phase="Failed", ready=False)]
        ]

        _failure(ctx, cluster, monkeypatch, match="listener gco-system/.* failed before serving")

        record = ctx.checkpoint.state["network_posture"][REGION]
        assert [(job["namespace"], job["deleted"]) for job in record["jobs"]] == [
            ("gco-system", True)
        ]

    def test_listener_that_never_turns_ready(self, monkeypatch: pytest.MonkeyPatch) -> None:
        ctx = _context()
        cluster = _FakeCluster()
        cluster.listener_frames["gco-jobs"] = [[_pod(f"{TARGET_JOB}-pod", ready=False)]]

        _failure(
            ctx,
            cluster,
            monkeypatch,
            match=f"listener gco-jobs/{TARGET_JOB} readiness did not happen within 600s",
            step=checks._POD_TIMEOUT_SECONDS,
        )

    def test_model_listener_that_never_turns_ready(self, monkeypatch: pytest.MonkeyPatch) -> None:
        ctx = _context()
        cluster = _FakeCluster()
        cluster.listener_frames["gco-inference"] = [[_pod(f"{MODEL_JOB}-pod", ready=False)]]

        _failure(
            ctx,
            cluster,
            monkeypatch,
            match=f"listener gco-inference/{MODEL_JOB} readiness did not happen within 600s",
            step=checks._POD_TIMEOUT_SECONDS,
        )

        record = ctx.checkpoint.state["network_posture"][REGION]
        assert [(job["namespace"], job["deleted"]) for job in record["jobs"]] == [
            ("gco-system", True),
            ("gco-jobs", True),
            ("gco-inference", True),
        ]

    def test_probe_that_never_finishes(self, monkeypatch: pytest.MonkeyPatch) -> None:
        ctx = _context()
        cluster = _FakeCluster()
        cluster.probe_frames["metrics-open"] = [
            [_pod(f"gco-live-netpol-metrics-open-{TOKEN}-pod", phase="Pending", ready=False)]
        ]

        _failure(
            ctx,
            cluster,
            monkeypatch,
            match="probe default/gco-live-netpol-metrics-open-.* completion did not happen",
            step=checks._POD_TIMEOUT_SECONDS,
        )

    @pytest.mark.parametrize(
        "items",
        [
            [],
            [_pod("inference-monitor-x", deleting=True, ip=MONITOR_IP)],
            [_pod("inference-monitor-x", phase="Pending", ip=MONITOR_IP)],
            [_pod("inference-monitor-x", containers=False, ip=MONITOR_IP)],
            [_pod("inference-monitor-x", ready=False, ip=MONITOR_IP)],
            [_pod("inference-monitor-x", ip=None)],
            ["not-a-pod"],
        ],
    )
    def test_no_ready_inference_monitor_to_probe(
        self, monkeypatch: pytest.MonkeyPatch, items: list[Any]
    ) -> None:
        ctx = _context()
        cluster = _FakeCluster()
        cluster.monitor_items = items

        _failure(ctx, cluster, monkeypatch, match="no ready inference-monitor pod to probe")

        # Every listener had been created and is torn down again.
        assert len([entry for entry in cluster.deleted if entry[0] == "cleanup"]) == 3

    def test_no_ready_cost_monitor_to_probe(self, monkeypatch: pytest.MonkeyPatch) -> None:
        ctx = _context()
        cluster = _FakeCluster()
        cluster.cost_items = [_pod("cost-monitor-x", ready=False, ip=COST_IP)]

        _failure(ctx, cluster, monkeypatch, match="no ready cost-monitor pod to probe")

        assert len([entry for entry in cluster.deleted if entry[0] == "cleanup"]) == 3
        ctx.aws_client.make_authenticated_request.assert_not_called()

    def test_clean_slate_delete_failure(self, monkeypatch: pytest.MonkeyPatch) -> None:
        ctx = _context()
        cluster = _FakeCluster()
        cluster.clear_failures.add(TARGET_JOB)

        _failure(
            ctx, cluster, monkeypatch, match=f"could not clear a previous gco-system/{TARGET_JOB}"
        )

        record = ctx.checkpoint.state["network_posture"][REGION]
        assert record["last_kubectl_error"]["argv"] == ["delete", "job", TARGET_JOB]
        assert cluster.applied == []

    def test_apply_failure(self, monkeypatch: pytest.MonkeyPatch) -> None:
        ctx = _context()
        cluster = _FakeCluster()
        cluster.apply_failures.add(f"gco-live-netpol-cross-jobs-{TOKEN}")

        _failure(
            ctx, cluster, monkeypatch, match="could not create default/gco-live-netpol-cross-jobs"
        )

        record = ctx.checkpoint.state["network_posture"][REGION]
        assert record["last_kubectl_error"]["returncode"] == 1
        assert all(job["deleted"] for job in record["jobs"])

    def test_cleanup_failure_fails_a_matched_matrix(self, monkeypatch: pytest.MonkeyPatch) -> None:
        ctx = _context()
        cluster = _FakeCluster()
        cluster.delete_failures.add(f"gco-live-netpol-http-egress-{TOKEN}")

        _failure(ctx, cluster, monkeypatch, match="1 probe Job\\(s\\) could not be deleted")

        record = ctx.checkpoint.state["network_posture"][REGION]
        stuck = [job for job in record["jobs"] if not job["deleted"]]
        assert [job["name"] for job in stuck] == [f"gco-live-netpol-http-egress-{TOKEN}"]
        assert "evidence" not in record

    def test_cleanup_exception_never_masks_the_verdict(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        ctx = _context()
        cluster = _FakeCluster(dict.fromkeys(PROBE_NAMES, ("Succeeded", 0)))
        original = cluster._delete

        def exploding(args: tuple[str, ...]) -> tuple[int, str, str]:
            if "--wait=false" in args:
                raise OSError("tunnel closed")
            return original(args)

        monkeypatch.setattr(cluster, "_delete", exploding)

        _failure(ctx, cluster, monkeypatch, match="expected blocked, observed reachable")

    def test_cleanup_exception_after_a_matched_matrix_still_fails(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        ctx = _context()
        cluster = _FakeCluster()
        original = cluster._delete

        def exploding(args: tuple[str, ...]) -> tuple[int, str, str]:
            if "--wait=false" in args:
                raise OSError("tunnel closed")
            return original(args)

        monkeypatch.setattr(cluster, "_delete", exploding)

        _failure(ctx, cluster, monkeypatch, match="1 probe Job\\(s\\) could not be deleted")

    def test_kubectl_read_failures_propagate(self, monkeypatch: pytest.MonkeyPatch) -> None:
        ctx = _context()
        cluster = _FakeCluster()

        def broken(*args: str, timeout: float, **kwargs: Any) -> tuple[int, str, str]:
            if args[0] == "get":
                return 1, "", "Unable to connect to the server"
            return cluster(*args, timeout=timeout, **kwargs)

        _install_clock(monkeypatch)
        with pytest.raises(KubectlError):
            checks.NetworkPostureProbe(ctx, REGION, broken).run()
        # The listener Job that was created before the read broke is deleted.
        assert ("cleanup", "gco-system", TARGET_JOB) in cluster.deleted


class TestDisruptions:
    """A disruption takes a verdict away; it never becomes one.

    A live run lost the HTTPS egress probe to an EKS Auto Mode interruption
    46 seconds into its sampling: the pod was evicted and deleted, the Job
    failed, and the harness waited out the whole pod timeout for a pod that
    no longer existed before reporting a timeout instead of a verdict.
    """

    def test_a_probe_whose_pod_was_evicted_and_deleted_is_rerun_at_once(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        ctx = _context()
        cluster = _FakeCluster()
        job = f"gco-live-netpol-https-egress-{TOKEN}"
        cluster.probe_frames["https-egress"] = [[_pod(f"{job}-pod")], []]
        cluster.job_conditions["https-egress"] = list(_JOB_FAILED)
        clock = _install_clock(monkeypatch)

        evidence = checks.NetworkPostureProbe(ctx, REGION, cluster).run()

        https = next(item for item in evidence["probes"] if item["name"] == "https-egress")
        assert https["status"] == "matched"
        assert https["observed"] == "reachable"
        assert https["pod"] == f"{job}-rerun"
        failed = {"reason": "BackoffLimitExceeded", "message": _JOB_FAILED[1]["message"]}
        assert https["disruptions"] == [{"attempt": 1, "pod": None, **failed}]
        others = [
            item for item in evidence["probes"] if item["name"] not in ("https-egress", API_PROBE)
        ]
        assert all(item["disruptions"] == [] for item in others)
        record = ctx.checkpoint.state["network_posture"][REGION]
        assert record["disruptions"] == [{"probe": "https-egress", "attempt": 1, **failed}]
        # Read at once rather than after the pod timeout: one poll saw the pod
        # running, the next found the Job failed with nothing left to read.
        assert len(clock.sleeps) == 1
        # The re-run is a fresh Job under the same record: cleared again (which
        # waits for the old pod), applied again, and deleted once at the end.
        assert [entry for entry in cluster.deleted if entry[2] == job] == [
            ("clear", "gco-jobs", job),
            ("clear", "gco-jobs", job),
            ("cleanup", "gco-jobs", job),
        ]
        assert [item["metadata"]["name"] for item in cluster.applied].count(job) == 2
        assert [entry["name"] for entry in record["jobs"]].count(job) == 1
        assert len(record["jobs"]) == 15
        assert all(entry["deleted"] for entry in record["jobs"])

    def test_a_second_disruption_is_reported_as_one_not_as_a_verdict(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        ctx = _context()
        cluster = _FakeCluster()
        job = f"gco-live-netpol-https-egress-{TOKEN}"
        cluster.probe_frames["https-egress"] = [[]]
        cluster.job_conditions["https-egress"] = list(_JOB_FAILED)
        # The fresh Job's pod is killed mid-sampling by a drain: an exit code
        # the script never chose, on a pod marked DisruptionTarget.
        cluster.rerun_frames["https-egress"] = [
            [_pod(f"{job}-rerun", phase="Failed", exit_code=137, conditions=_evicted())]
        ]
        cluster.logs[f"{job}-rerun"] = ("NETPOL_SAMPLE t=0s reachable\n", "")

        _failure(
            ctx,
            cluster,
            monkeypatch,
            match=(
                r"https-egress \(gco-jobs -> https://checkip\.amazonaws\.com/\) expected "
                r"reachable, observed error \[phase=Failed exit=137\] after 2 disruption\(s\), "
                r"last: EvictionByEvictionAPI"
            ),
        )

        record = ctx.checkpoint.state["network_posture"][REGION]
        https = next(item for item in record["probes"] if item["name"] == "https-egress")
        assert https["status"] == "mismatch"
        assert https["samples"] == ["t=0s reachable"]
        assert [
            (entry["attempt"], entry["pod"], entry["reason"]) for entry in https["disruptions"]
        ] == [(1, None, "BackoffLimitExceeded"), (2, f"{job}-rerun", "EvictionByEvictionAPI")]
        assert https["disruptions"][1]["message"] == "Eviction API: evicting"
        assert [entry["probe"] for entry in record["disruptions"]] == ["https-egress"] * 2
        # Two attempts, never a third.
        assert [item["metadata"]["name"] for item in cluster.applied].count(job) == 2
        assert all(entry["deleted"] for entry in record["jobs"])

    @pytest.mark.parametrize(
        ("probe", "phase", "exit_code"),
        [("cross-jobs", "Failed", 42), ("same-namespace", "Succeeded", 0)],
    )
    def test_a_verdict_the_script_reached_stands_on_a_pod_marked_for_disruption(
        self, monkeypatch: pytest.MonkeyPatch, probe: str, phase: str, exit_code: int
    ) -> None:
        """A drain that lands after the script chose its exit code takes nothing away."""
        ctx = _context()
        cluster = _FakeCluster()
        job = f"gco-live-netpol-{probe}-{TOKEN}"
        cluster.probe_frames[probe] = [
            [_pod(f"{job}-pod", phase=phase, exit_code=exit_code, conditions=_evicted())]
        ]

        evidence = _run(ctx, cluster, monkeypatch)

        result = next(item for item in evidence["probes"] if item["name"] == probe)
        assert result["status"] == "matched"
        assert result["disruptions"] == []
        assert [item["metadata"]["name"] for item in cluster.applied].count(job) == 1
        assert "disruptions" not in ctx.checkpoint.state["network_posture"][REGION]

    @pytest.mark.parametrize(
        ("namespace", "final_frame"),
        [
            ("gco-jobs", []),
            ("gco-jobs", [_pod(f"{TARGET_JOB}-pod", deleting=True, ip="10.0.2.7")]),
            ("gco-system", [_pod(f"{TARGET_JOB}-replacement", ip="10.0.1.6")]),
            ("gco-system", [_pod(f"{TARGET_JOB}-pod", phase="Failed", ready=False)]),
            ("gco-jobs", [_pod(f"{TARGET_JOB}-pod", ip="10.0.2.7", conditions=_evicted())]),
            ("gco-inference", []),
        ],
        ids=["gone", "terminating", "replaced", "failed", "marked-for-disruption", "model-gone"],
    )
    def test_a_listener_that_did_not_last_the_matrix_voids_its_verdicts(
        self,
        monkeypatch: pytest.MonkeyPatch,
        namespace: str,
        final_frame: list[dict[str, Any]],
    ) -> None:
        ctx = _context()
        cluster = _FakeCluster()
        job = LISTENER_JOBS[namespace]
        cluster.listener_frames[namespace] = [
            [_pod(f"{job}-pod", ip=TARGET_IPS[namespace])],
            final_frame,
        ]

        _failure(
            ctx,
            cluster,
            monkeypatch,
            match=(
                rf"listener\(s\) {namespace}/{job} did not last the probe matrix "
                r"\(evicted or replaced\), so the verdicts dialed against them are void"
            ),
        )

        record = ctx.checkpoint.state["network_posture"][REGION]
        assert record["disrupted_listeners"] == [f"{namespace}/{job}"]
        # The verdicts are kept for the report, and everything is deleted.
        assert {item["status"] for item in record["probes"]} == {"matched"}
        assert all(entry["deleted"] for entry in record["jobs"])
        assert "evidence" not in record

    def test_a_replaced_inference_monitor_is_named_with_every_other_lost_listener(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        ctx = _context()
        cluster = _FakeCluster()
        cluster.listener_frames["gco-jobs"] = [
            [_pod(f"{TARGET_JOB}-pod", ip=TARGET_IPS["gco-jobs"])],
            [],
        ]
        # The metrics probes dialed inference-monitor-x; a rollout replaced it.
        cluster.monitor_frames = [[_pod("inference-monitor-x", ip=MONITOR_IP)]]
        cluster.monitor_items = [_pod("inference-monitor-y", ip="10.0.3.10")]

        _failure(
            ctx,
            cluster,
            monkeypatch,
            match=(
                rf"listener\(s\) gco-jobs/{TARGET_JOB}, gco-system/inference-monitor-x "
                "did not last the probe matrix"
            ),
        )

        record = ctx.checkpoint.state["network_posture"][REGION]
        assert record["disrupted_listeners"] == [
            f"gco-jobs/{TARGET_JOB}",
            "gco-system/inference-monitor-x",
        ]

    def test_a_replaced_cost_monitor_voids_the_verdicts_dialed_at_it(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        ctx = _context()
        cluster = _FakeCluster()
        cluster.cost_frames = [[_pod("cost-monitor-x", ip=COST_IP)]]
        cluster.cost_items = [_pod("cost-monitor-y", ip="10.0.5.4")]

        _failure(
            ctx,
            cluster,
            monkeypatch,
            match=r"listener\(s\) gco-system/cost-monitor-x did not last the probe matrix",
        )


def _shipped_network_policies() -> dict[tuple[str | None, str], dict[str, Any]]:
    """Every NetworkPolicy the applier ships in 03 and 34, keyed by namespace/name."""
    policies: dict[tuple[str | None, str], dict[str, Any]] = {}
    for filename in ("03-network-policies.yaml", "34-cost-monitor.yaml"):
        text = _PLACEHOLDER.sub("placeholder", (_MANIFESTS / filename).read_text(encoding="utf-8"))
        for document in yaml.safe_load_all(text):
            if isinstance(document, dict) and document.get("kind") == "NetworkPolicy":
                metadata = document["metadata"]
                policies[(metadata.get("namespace"), metadata["name"])] = document
    return policies


class TestShippedManifests:
    """The listeners and ports the matrix dials are the ones the shipped policies name."""

    def test_the_model_listener_is_selected_by_the_model_ingress_policies(self) -> None:
        ingress = _shipped_network_policies()[("gco-inference", "allow-inference-proxy-ingress")]
        assert ingress["spec"]["podSelector"] == {"matchLabels": {"gco.io/type": "inference"}}

    @pytest.mark.parametrize(
        ("namespace", "name", "port"),
        [
            (
                "gco-system",
                "allow-metrics-to-inference-monitor",
                checks._INFERENCE_MONITOR_METRICS_PORT,
            ),
            (
                "gco-system",
                "allow-manifest-processor-to-cost-monitor-ingress",
                checks._COST_MONITOR_PORT,
            ),
            ("gco-inference", "allow-inference-proxy-ingress", checks._MODEL_PORT),
        ],
    )
    def test_ingress_is_admitted_only_on_the_tls_port_the_matrix_dials(
        self, namespace: str, name: str, port: int
    ) -> None:
        policy = _shipped_network_policies()[(namespace, name)]
        ports = [entry for rule in policy["spec"]["ingress"] for entry in rule["ports"]]
        assert ports == [{"protocol": "TCP", "port": port}]


class TestAction:
    def test_visits_every_region_through_its_own_tunnel(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        ctx = _context(regions=("us-east-1", "eu-west-1"))
        opened: list[str] = []
        probes: list[tuple[Any, str, Any]] = []

        @contextlib.contextmanager
        def cluster_kubectl(context: Any, region: str):
            assert context is ctx
            opened.append(region)
            yield f"kubectl-{region}"

        class Probe:
            def __init__(self, context: Any, region: str, kubectl: Any) -> None:
                probes.append((context, region, kubectl))
                self.region = region

            def run(self) -> dict[str, Any]:
                return {"region": self.region}

        monkeypatch.setattr(action_module, "cluster_kubectl", cluster_kubectl)
        monkeypatch.setattr(action_module, "NetworkPostureProbe", Probe)

        evidence = action_module.action_network_posture(ctx)

        assert opened == ["us-east-1", "eu-west-1"]
        assert probes == [
            (ctx, "us-east-1", "kubectl-us-east-1"),
            (ctx, "eu-west-1", "kubectl-eu-west-1"),
        ]
        assert evidence == {
            "network_policy_enforcement": True,
            "cost_monitoring": True,
            "regions": {
                "us-east-1": {"region": "us-east-1"},
                "eu-west-1": {"region": "eu-west-1"},
            },
        }
