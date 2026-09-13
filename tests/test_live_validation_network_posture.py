"""
Tests for the live-validation network-posture action and its probe matrix.

Covers the matrix itself (which client dials which target with which verdict
promised by which shipped policy), the Job lifecycle around every probe (a
clean-slate foreground delete, apply from the checked-in manifest with the
run token and PROBE_URL substituted, bounded waits for a Ready listener or a
terminal probe, background delete of every recorded Job), verdict
classification from phase plus exit code, the enforcement-off skip of the
deny probes, the fail-closed handling of listener failures, missing
inference-monitor pods, kubectl failures, deadlines, and cleanup problems, the
checkpoint record that authorizes cleanup, and the action's per-Region tunnel
session. Every kubectl, tunnel, and clock boundary is faked; the manifests
are the real files.
"""

from __future__ import annotations

import contextlib
import json
import threading
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pytest

from cli.jobs import JobManager
from scripts.live_release_validation.actions import network_posture as action_module
from scripts.live_release_validation.checks import jobs as checks_jobs
from scripts.live_release_validation.checks import network_posture as checks
from scripts.live_release_validation.checks.cluster import KubectlError

REGION = "us-east-1"
TOKEN = checks_jobs._run_token("run-123")
TARGET_JOB = f"gco-live-netpol-target-{TOKEN}"
TARGET_IPS = {"gco-system": "10.0.1.5", "gco-jobs": "10.0.2.7"}
MONITOR_IP = "10.0.3.9"
PROBE_NAMES = (
    "same-namespace",
    "cross-jobs",
    "cross-system",
    "metrics-open",
    "https-egress",
    "http-egress",
)
DENY_PROBES = ("cross-jobs", "cross-system", "http-egress")
#: What a correctly enforcing cluster answers, per probe.
ENFORCED = {
    "same-namespace": ("Succeeded", 0),
    "cross-jobs": ("Failed", 42),
    "cross-system": ("Failed", 42),
    "metrics-open": ("Succeeded", 0),
    "https-egress": ("Succeeded", 0),
    "http-egress": ("Failed", 42),
}


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
        checkpoint=SimpleNamespace(state={} if state is None else state),
        state_lock=threading.RLock(),
        deployment_regions=regions,
        config=SimpleNamespace(project_name="gco-live"),
        cdk_context={} if cdk_context is None else cdk_context,
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
) -> dict[str, Any]:
    metadata: dict[str, Any] = {"name": name}
    if deleting:
        metadata["deletionTimestamp"] = "2026-09-10T00:00:00Z"
    status: dict[str, Any] = {"phase": phase}
    if ip is not None:
        status["podIP"] = ip
    if containers:
        container: dict[str, Any] = {"name": "c", "ready": ready}
        if exit_code is not None:
            container["state"] = {"terminated": {"exitCode": exit_code}}
        status["containerStatuses"] = [container]
    return {"metadata": metadata, "status": status}


def _probe_name(job_name: str) -> str:
    return job_name.removeprefix("gco-live-netpol-").removesuffix(f"-{TOKEN}")


class _FakeCluster:
    """Scripted kubectl for the probe matrix.

    ``apply`` materializes the Job's pod frames: listeners come up Ready with
    the namespace's target IP, probes terminate with the scripted verdict.
    Each ``get pods`` for a Job returns the next frame until the last one.
    """

    def __init__(self, verdicts: dict[str, tuple[str, int | None]] | None = None) -> None:
        self.verdicts: dict[str, tuple[str, int | None]] = dict(
            ENFORCED if verdicts is None else verdicts
        )
        self.frames: dict[tuple[str, str], list[list[dict[str, Any]]]] = {}
        self.listener_frames: dict[str, list[list[dict[str, Any]]]] = {}
        self.probe_frames: dict[str, list[list[dict[str, Any]]]] = {}
        self.monitor_items: list[dict[str, Any]] = [_pod("inference-monitor-x", ip=MONITOR_IP)]
        self.logs: dict[str, tuple[str, str]] = {}
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
        assert verb == "get" and args[1] == "pods" and args[-2:] == ("--output", "json")
        namespace = args[args.index("--namespace") + 1]
        selector = args[args.index("--selector") + 1]
        if selector == "app=inference-monitor":
            return 0, json.dumps({"items": self.monitor_items}), ""
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
        if name == TARGET_JOB:
            frames = self.listener_frames.get(
                namespace, [[_pod(f"{name}-pod", ip=TARGET_IPS[namespace])]]
            )
        else:
            probe = _probe_name(name)
            phase, exit_code = self.verdicts[probe]
            frames = self.probe_frames.get(
                probe, [[_pod(f"{name}-pod", phase=phase, exit_code=exit_code)]]
            )
            verdict = "REACHABLE" if phase == "Succeeded" else "BLOCKED"
            self.logs.setdefault(f"{name}-pod", (f"NETPOL_{verdict}\n", ""))
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


class TestProbeMatrix:
    def test_every_promise_of_the_shipped_policies_has_a_probe(self) -> None:
        specs = checks._probe_specs("1.1.1.1", "2.2.2.2", "3.3.3.3")
        assert [spec.name for spec in specs] == list(PROBE_NAMES)
        by_name = {spec.name: spec for spec in specs}
        assert by_name["same-namespace"].url == "http://2.2.2.2:8080/"
        assert by_name["cross-jobs"].client_namespace == "default"
        assert by_name["cross-system"].url == "http://1.1.1.1:8080/"
        assert by_name["metrics-open"].url == "http://3.3.3.3:9090/metrics"
        assert by_name["https-egress"].url == "https://checkip.amazonaws.com/"
        assert by_name["http-egress"].url == "http://checkip.amazonaws.com/"
        assert {spec.name for spec in specs if spec.expected == "blocked"} == set(DENY_PROBES)
        assert {spec.name for spec in specs if spec.enforcement_only} == set(DENY_PROBES)
        for spec in specs:
            assert spec.expected in ("reachable", "blocked")
            assert len(f"gco-live-netpol-{spec.name}-{TOKEN}") <= 63
            assert spec.rule

    def test_probe_names_are_kubernetes_safe(self) -> None:
        for name in PROBE_NAMES:
            assert name == name.lower() and set(name) <= set("abcdefghijklmnopqrstuvwxyz-")


class TestEnforcedCluster:
    def test_matrix_passes_and_leaves_nothing_behind(self, monkeypatch: pytest.MonkeyPatch) -> None:
        ctx = _context()
        cluster = _FakeCluster()

        evidence = _run(ctx, cluster, monkeypatch)

        assert evidence["enforcement_configured"] is True
        assert evidence["targets"] == {
            "gco-system": {"job": TARGET_JOB, "pod": f"{TARGET_JOB}-pod", "ip": "10.0.1.5"},
            "gco-jobs": {"job": TARGET_JOB, "pod": f"{TARGET_JOB}-pod", "ip": "10.0.2.7"},
        }
        assert evidence["inference_monitor"] == {"pod": "inference-monitor-x", "ip": MONITOR_IP}
        assert [probe["name"] for probe in evidence["probes"]] == list(PROBE_NAMES)
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

        # Two listeners then six probes, each cleared before creation and
        # deleted afterwards; the record holds every Job before it exists.
        created = [
            (job["metadata"]["namespace"], job["metadata"]["name"]) for job in cluster.applied
        ]
        assert created[:2] == [("gco-system", TARGET_JOB), ("gco-jobs", TARGET_JOB)]
        assert created[2:] == [
            ("gco-jobs", f"gco-live-netpol-same-namespace-{TOKEN}"),
            ("default", f"gco-live-netpol-cross-jobs-{TOKEN}"),
            ("gco-jobs", f"gco-live-netpol-cross-system-{TOKEN}"),
            ("default", f"gco-live-netpol-metrics-open-{TOKEN}"),
            ("gco-jobs", f"gco-live-netpol-https-egress-{TOKEN}"),
            ("gco-jobs", f"gco-live-netpol-http-egress-{TOKEN}"),
        ]
        assert [entry[1:] for entry in cluster.deleted if entry[0] == "clear"] == created
        assert [entry[1:] for entry in cluster.deleted if entry[0] == "cleanup"] == created
        record = ctx.checkpoint.state["network_posture"][REGION]
        assert [(job["namespace"], job["name"]) for job in record["jobs"]] == created
        assert all(job["deleted"] for job in record["jobs"])
        assert record["evidence"] is evidence
        assert record["targets"] == evidence["targets"]
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
        container = listener["spec"]["template"]["spec"]["containers"][0]
        assert container["image"].startswith("docker.io/library/busybox:1.38.0@sha256:")
        assert "httpd -f -p 8080" in container["command"][-1]
        assert container["readinessProbe"]["exec"]["command"][-1] == "http://127.0.0.1:8080/"
        assert "env" not in container
        assert listener["spec"]["activeDeadlineSeconds"] == 1800
        assert listener["spec"]["ttlSecondsAfterFinished"] == 600

        probe = cluster.applied[2]
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
        cluster = _FakeCluster(
            dict.fromkeys(PROBE_NAMES, ("Succeeded", 0))  # nothing is denied any more
        )

        evidence = _run(ctx, cluster, monkeypatch)

        assert evidence["enforcement_configured"] is False
        statuses = {probe["name"]: probe["status"] for probe in evidence["probes"]}
        assert statuses == {
            "cross-jobs": "skipped",
            "cross-system": "skipped",
            "http-egress": "skipped",
            "same-namespace": "matched",
            "metrics-open": "matched",
            "https-egress": "matched",
        }
        skipped = next(probe for probe in evidence["probes"] if probe["name"] == "cross-jobs")
        assert "network_policy_enforcement is false" in skipped["reason"]
        assert "observed" not in skipped
        launched = {_probe_name(job["metadata"]["name"]) for job in cluster.applied[2:]}
        assert launched == {"same-namespace", "metrics-open", "https-egress"}


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
        assert "same-namespace" not in failure
        # Everything the run created was still deleted.
        assert all(job["deleted"] for job in record["jobs"])
        assert len(record["jobs"]) == 8
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

        # Both listeners had been created and are torn down again.
        assert len([entry for entry in cluster.deleted if entry[0] == "cleanup"]) == 2

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
            "regions": {
                "us-east-1": {"region": "us-east-1"},
                "eu-west-1": {"region": "eu-west-1"},
            },
        }
