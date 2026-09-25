"""Network posture probes: prove the shipped NetworkPolicies decide traffic live.

``03-network-policies.yaml`` documents a zero-trust posture, and on EKS Auto
Mode that posture is only real once ``06-network-policy-controller.yaml`` has
switched the policy controller on. The ``network-posture`` action settles the
question on the deployed cluster by dialing real connections from throwaway
pods and reading the verdict from each probe's exit code:

* ``gco-jobs`` -> ``gco-jobs``: **reachable** (``allow-same-namespace``, so a
  multi-pod job can talk to itself);
* ``default`` -> ``gco-jobs``: **blocked** (``default-deny-ingress``);
* ``gco-jobs`` -> ``gco-system``: **blocked** (``default-deny-ingress`` — the
  job namespace cannot reach the control plane's services);
* ``default`` -> the live inference-monitor's ``:9090/metrics``: **reachable**
  (``allow-metrics-to-inference-monitor`` admits exactly the metrics port,
  which is what lets Prometheus scrape it);
* ``gco-jobs`` -> ``https://checkip.amazonaws.com/``: **reachable**
  (``allow-dns`` plus ``allow-https-egress``);
* ``gco-jobs`` -> ``http://checkip.amazonaws.com/`` on port 80: **blocked**
  (no rule admits it — the same host over 443 answered, so this is the policy
  deciding, not the network); and
* ``gco-jobs`` -> ``http://169.254.170.23/v1/credentials``: **reachable**
  (``allow-pod-identity-agent`` — the node's EKS Pod Identity Agent, which is
  where a ``gco-service-account`` pod gets its AWS credentials; the probe
  carries no token, so the agent answers 4xx, and any HTTP answer counts).
  Port 80 to the internet is blocked by the previous probe, so this one shows
  the rule admitting exactly the link-local endpoint.

Every probe is a digest-pinned BusyBox Job labelled with this run's token
(``manifests/netpol-probe-job.yaml``); the two listeners are the same image
behind ``httpd`` (``manifests/netpol-target-job.yaml``). All of them are
deleted before the action returns, and each carries ``activeDeadlineSeconds``
plus a TTL so a harness that dies mid-probe still leaves nothing behind.

A probe's verdict is its steady state, not its first dial. The VPC CNI
attaches a new pod's policies in parallel with the pod's start and admits
everything until they are in place (standard mode; Auto Mode's NodeClass
``networkPolicy: DefaultDeny`` is the strict alternative, which requires a
policy for every pod on the node), so a client that dials once at start can
read that window instead of the policy — the fourth live run of this branch
saw exactly that on the port-80 egress probe. The probe script therefore
samples until one answer has held for 30 seconds and at least 45 seconds have
passed, exits 43 if the answer is still changing after three minutes, and
prints every change of answer; the harness keeps those lines as ``samples``,
the closing line as ``settled``, and flags ``attach_window_observed`` when the
first answer differed from the steady state.

A disruption is not a verdict. Probe and listener pods ask Karpenter to leave
their node alone (``karpenter.sh/do-not-disrupt``) and prefer on-demand
capacity, but an interruption — a Spot reclaim, scheduled maintenance — can
still evict one: a live run lost the HTTPS egress probe 46 seconds into its
sampling and then waited out the whole pod timeout on a pod that no longer
existed. A probe whose Job failed with no terminated pod left, or whose pod
carries ``DisruptionTarget`` without a verdict exit code, is read at once and
re-run once from a fresh Job; a second disruption is reported as one. A
listener pod that did not last the matrix (either ``httpd`` target, or the
inference-monitor pod the metrics probe dialed) voids every verdict dialed
against it, so the action names it instead of reporting mismatches.

When cdk.json turns the controller off (``eks_cluster.network_policy_enforcement:
false``) the deny verdicts are not promised and those probes are recorded as
skipped; the reachability probes still have to pass.
"""

from __future__ import annotations

import copy
import json
import time
from dataclasses import asdict, dataclass
from typing import Any

from ..models import RunContext
from .cluster import KubectlRunner, kubectl_json
from .jobs import _load_manifest, _run_token
from .platform_workloads import network_policy_enforcement_enabled

_TARGET_MANIFEST = "netpol-target-job.yaml"
_PROBE_MANIFEST = "netpol-probe-job.yaml"
_TARGET_PORT = 8080
_INFERENCE_MONITOR_METRICS_PORT = 9090
_EGRESS_HOST = "checkip.amazonaws.com"
#: The EKS Pod Identity Agent's link-local address on every node; the
#: cluster points AWS_CONTAINER_CREDENTIALS_FULL_URI at this path.
_POD_IDENTITY_AGENT_URL = "http://169.254.170.23/v1/credentials"
#: A namespace the manifests leave unpoliced, so a client there proves the
#: target namespace's ingress rules and nothing else.
_UNPOLICED_NAMESPACE = "default"
_REACHABLE_EXIT_CODE = 0
_BLOCKED_EXIT_CODE = 42
#: The answer kept changing for the probe's whole budget; never a verdict.
_UNSETTLED_EXIT_CODE = 43
#: Exit codes the probe script chose; anything else means it never finished.
_SCRIPT_EXIT_CODES = frozenset({_REACHABLE_EXIT_CODE, _BLOCKED_EXIT_CODE, _UNSETTLED_EXIT_CODE})
#: A probe a disruption voided is re-run from a fresh Job, once.
_PROBE_ATTEMPTS = 2
#: Auto Mode may have to launch a node for the first pod; a probe then still
#: has the image pull and its own three-minute sampling budget ahead of it.
_POD_TIMEOUT_SECONDS = 600
#: One line per change of answer plus the closing lines; an answer that
#: flapped on every sample for the whole budget prints more, and the tail
#: keeps the end of that story.
_LOG_TAIL_LINES = 40
_LOG_LIMIT = 4_000
_SAMPLE_PREFIX = "NETPOL_SAMPLE "
_SETTLED_PREFIXES = ("NETPOL_SETTLED ", "NETPOL_UNSETTLED ")


class NetworkPostureValidationError(RuntimeError):
    """The live cluster does not enforce the documented network posture."""


@dataclass(frozen=True)
class ProbeSpec:
    """One connection attempt and the verdict the shipped policies promise."""

    name: str
    client_namespace: str
    url: str
    expected: str
    rule: str
    #: A deny verdict exists only while the policy controller is on.
    enforcement_only: bool


def _probe_specs(
    system_target_ip: str,
    jobs_target_ip: str,
    inference_monitor_ip: str,
) -> tuple[ProbeSpec, ...]:
    return (
        ProbeSpec(
            "same-namespace",
            "gco-jobs",
            f"http://{jobs_target_ip}:{_TARGET_PORT}/",
            "reachable",
            "gco-jobs/allow-same-namespace",
            enforcement_only=False,
        ),
        ProbeSpec(
            "cross-jobs",
            _UNPOLICED_NAMESPACE,
            f"http://{jobs_target_ip}:{_TARGET_PORT}/",
            "blocked",
            "gco-jobs/default-deny-ingress",
            enforcement_only=True,
        ),
        ProbeSpec(
            "cross-system",
            "gco-jobs",
            f"http://{system_target_ip}:{_TARGET_PORT}/",
            "blocked",
            "gco-system/default-deny-ingress",
            enforcement_only=True,
        ),
        ProbeSpec(
            "metrics-open",
            _UNPOLICED_NAMESPACE,
            f"http://{inference_monitor_ip}:{_INFERENCE_MONITOR_METRICS_PORT}/metrics",
            "reachable",
            "gco-system/allow-metrics-to-inference-monitor",
            enforcement_only=False,
        ),
        ProbeSpec(
            "https-egress",
            "gco-jobs",
            f"https://{_EGRESS_HOST}/",
            "reachable",
            "gco-jobs/allow-dns + gco-jobs/allow-https-egress",
            enforcement_only=False,
        ),
        ProbeSpec(
            "http-egress",
            "gco-jobs",
            f"http://{_EGRESS_HOST}/",
            "blocked",
            "gco-jobs egress (no rule admits port 80)",
            enforcement_only=True,
        ),
        ProbeSpec(
            "pod-identity-agent",
            "gco-jobs",
            _POD_IDENTITY_AGENT_URL,
            "reachable",
            "gco-jobs/allow-pod-identity-agent",
            enforcement_only=False,
        ),
    )


def _dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def _substitute(value: Any, replacements: dict[str, str]) -> Any:
    """Replace manifest placeholders (``__PROBE_URL__``) wherever they appear."""
    if isinstance(value, str):
        for token, replacement in replacements.items():
            value = value.replace(token, replacement)
        return value
    if isinstance(value, list):
        return [_substitute(item, replacements) for item in value]
    if isinstance(value, dict):
        return {key: _substitute(item, replacements) for key, item in value.items()}
    return value


class NetworkPostureProbe:
    """Run the probe matrix on one Region's cluster and clean up after it."""

    def __init__(self, ctx: RunContext, region: str, kubectl: KubectlRunner) -> None:
        self.ctx = ctx
        self.region = region
        self.kubectl = kubectl
        self.token = _run_token(ctx.settings.run_id)
        self.timeout = float(ctx.settings.command_timeout_seconds)
        with ctx.state_lock:
            self.record: dict[str, Any] = ctx.checkpoint.state.setdefault(
                "network_posture", {}
            ).setdefault(region, {})
            self.record.setdefault("jobs", [])

    # -- checkpointing -----------------------------------------------------

    def _persist(self) -> None:
        self.ctx.persist()

    def _fail(self, message: str) -> NetworkPostureValidationError:
        self.record["failure"] = message
        self._persist()
        return NetworkPostureValidationError(f"network posture in {self.region}: {message}")

    # -- Job lifecycle -----------------------------------------------------

    def _job_manifest(
        self,
        filename: str,
        *,
        name: str,
        namespace: str,
        replacements: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        """Load one checked-in Job (run token applied) and give it this probe's identity."""
        manifests, _name, _namespace = _load_manifest(self.ctx, filename)
        job = copy.deepcopy(next(item for item in manifests if item.get("kind") == "Job"))
        if replacements:
            job = _substitute(job, replacements)
        job["metadata"]["name"] = name
        job["metadata"]["namespace"] = namespace
        return job

    def _run(self, *arguments: str, **kwargs: Any) -> tuple[int, str, str]:
        code, stdout, stderr = self.kubectl(*arguments, timeout=self.timeout, **kwargs)
        return code, stdout, stderr

    def _create_job(self, job: dict[str, Any]) -> None:
        namespace = job["metadata"]["namespace"]
        name = job["metadata"]["name"]
        # Record before creating: a Job that exists without a record could not
        # be cleaned up, so the record is what authorizes the delete. A re-run
        # of a disrupted probe reuses its Job's record.
        entry = {"namespace": namespace, "name": name, "deleted": False}
        if entry not in self.record["jobs"]:
            self.record["jobs"].append(entry)
        self._persist()
        # Start from a clean slate so a retry of this run never reads a stale
        # verdict; foreground cascading waits for the old pod to be gone.
        code, _stdout, stderr = self._run(
            "delete",
            "job",
            name,
            "--namespace",
            namespace,
            "--ignore-not-found",
            "--cascade=foreground",
            "--wait=true",
            "--timeout=120s",
        )
        if code != 0:
            self.record["last_kubectl_error"] = {
                "argv": ["delete", "job", name],
                "returncode": code,
                "stderr": stderr[-_LOG_LIMIT:],
            }
            raise self._fail(f"could not clear a previous {namespace}/{name}")
        code, _stdout, stderr = self._run(
            "apply", "--namespace", namespace, "--filename", "-", input=json.dumps(job)
        )
        if code != 0:
            self.record["last_kubectl_error"] = {
                "argv": ["apply", namespace, name],
                "returncode": code,
                "stderr": stderr[-_LOG_LIMIT:],
            }
            raise self._fail(f"could not create {namespace}/{name}")

    def _live_pods(self, namespace: str, selector: str) -> list[dict[str, Any]]:
        """The selector's pods that are not being deleted."""
        payload = kubectl_json(
            self.kubectl,
            self.record,
            "get",
            "pods",
            "--namespace",
            namespace,
            "--selector",
            selector,
            timeout=self.timeout,
        )
        return [
            _dict(item)
            for item in _list(_dict(payload).get("items"))
            if not _dict(_dict(item).get("metadata")).get("deletionTimestamp")
        ]

    def _job_pod(self, namespace: str, name: str) -> dict[str, Any] | None:
        pods = self._live_pods(namespace, f"job-name={name}")
        return pods[0] if pods else None

    def _job_failure(self, namespace: str, name: str) -> dict[str, str] | None:
        """The Job's ``Failed`` condition as ``{reason, message}``, or ``None``."""
        payload = kubectl_json(
            self.kubectl,
            self.record,
            "get",
            "job",
            name,
            "--namespace",
            namespace,
            timeout=self.timeout,
        )
        for entry in _list(_dict(_dict(payload).get("status")).get("conditions")):
            condition = _dict(entry)
            if condition.get("type") == "Failed" and condition.get("status") == "True":
                return {
                    "reason": str(condition.get("reason") or "Failed"),
                    "message": str(condition.get("message") or "")[:_LOG_LIMIT],
                }
        return None

    @staticmethod
    def _disruption_condition(pod: dict[str, Any]) -> dict[str, str] | None:
        """The pod's ``DisruptionTarget`` condition as ``{reason, message}``, or ``None``."""
        for entry in _list(_dict(pod.get("status")).get("conditions")):
            condition = _dict(entry)
            if condition.get("type") == "DisruptionTarget" and condition.get("status") == "True":
                return {
                    "reason": str(condition.get("reason") or "DisruptionTarget"),
                    "message": str(condition.get("message") or "")[:_LOG_LIMIT],
                }
        return None

    def _wait(self, deadline: float, what: str) -> None:
        if time.monotonic() >= deadline:
            raise self._fail(f"{what} did not happen within {_POD_TIMEOUT_SECONDS}s")
        time.sleep(self.ctx.settings.poll_interval_seconds)

    def _wait_for_listener(self, namespace: str, name: str) -> tuple[str, str]:
        """Return ``(pod name, pod IP)`` once the listener pod is Running and Ready."""
        deadline = time.monotonic() + _POD_TIMEOUT_SECONDS
        while True:
            pod = self._job_pod(namespace, name)
            status = _dict(pod.get("status")) if pod else {}
            containers = [_dict(entry) for entry in _list(status.get("containerStatuses"))]
            if status.get("phase") == "Failed":
                raise self._fail(f"listener {namespace}/{name} failed before serving")
            if (
                pod is not None
                and status.get("phase") == "Running"
                and containers
                and all(entry.get("ready") is True for entry in containers)
                and status.get("podIP")
            ):
                return str(_dict(pod.get("metadata")).get("name")), str(status["podIP"])
            self._wait(deadline, f"listener {namespace}/{name} readiness")

    def _wait_for_verdict(self, namespace: str, name: str) -> dict[str, Any]:
        """Return the probe pod's terminal phase, exit code, log tail, and disruption.

        ``disruption`` is ``None`` for a verdict. It holds the reason when a
        disruption took the verdict away: the Job failed with no terminated pod
        left to read (the pod was evicted and deleted), or the pod stopped with
        an exit code the script never chose while carrying ``DisruptionTarget``.
        Both return at once instead of waiting out the deadline.
        """
        deadline = time.monotonic() + _POD_TIMEOUT_SECONDS
        while True:
            pod = self._job_pod(namespace, name)
            status = _dict(pod.get("status")) if pod else {}
            phase = status.get("phase")
            if pod is not None and phase in ("Succeeded", "Failed"):
                containers = [_dict(entry) for entry in _list(status.get("containerStatuses"))]
                terminated = (
                    _dict(_dict(containers[0].get("state")).get("terminated")) if containers else {}
                )
                exit_code = terminated.get("exitCode")
                pod_name = str(_dict(pod.get("metadata")).get("name"))
                _code, stdout, stderr = self._run(
                    "logs",
                    pod_name,
                    "--namespace",
                    namespace,
                    f"--tail={_LOG_TAIL_LINES}",
                )
                disruption = (
                    None if exit_code in _SCRIPT_EXIT_CODES else self._disruption_condition(pod)
                )
                return {
                    "pod": pod_name,
                    "phase": phase,
                    "exit_code": exit_code,
                    "output": (stdout or stderr)[-_LOG_LIMIT:],
                    "disruption": disruption,
                }
            if pod is None and (failure := self._job_failure(namespace, name)) is not None:
                return {
                    "pod": None,
                    "phase": "Failed",
                    "exit_code": None,
                    "output": "",
                    "disruption": failure,
                }
            self._wait(deadline, f"probe {namespace}/{name} completion")

    def _probe_verdict(
        self, spec: ProbeSpec, job: dict[str, Any]
    ) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        """Wait for one probe, re-running it from a fresh Job if a disruption voided it."""
        namespace = job["metadata"]["namespace"]
        name = job["metadata"]["name"]
        disruptions: list[dict[str, Any]] = []
        for attempt in range(1, _PROBE_ATTEMPTS + 1):
            verdict = self._wait_for_verdict(namespace, name)
            disruption = verdict.pop("disruption")
            if disruption is None:
                break
            disruptions.append({"attempt": attempt, "pod": verdict["pod"], **disruption})
            self.record.setdefault("disruptions", []).append(
                {"probe": spec.name, "attempt": attempt, **disruption}
            )
            self._persist()
            if attempt < _PROBE_ATTEMPTS:
                self._create_job(job)
        return verdict, disruptions

    def _disrupted_listeners(
        self, targets: dict[str, dict[str, str]], monitor_pod: str
    ) -> list[str]:
        """Listeners whose pod did not last the matrix; verdicts dialed at them are void.

        The inference-monitor pod the metrics probe dialed is one of them: a
        replaced pod answers on a different IP, so the probe that dialed the
        old one would read ``blocked`` for a reason no policy chose.
        """
        dialed = [
            (namespace, f"job-name={target['job']}", target["pod"], f"{namespace}/{target['job']}")
            for namespace, target in targets.items()
        ]
        dialed.append(
            ("gco-system", "app=inference-monitor", monitor_pod, f"gco-system/{monitor_pod}")
        )
        broken: list[str] = []
        for namespace, selector, pod_name, label in dialed:
            pod = next(
                (
                    item
                    for item in self._live_pods(namespace, selector)
                    if _dict(item.get("metadata")).get("name") == pod_name
                ),
                None,
            )
            if (
                pod is None
                or _dict(pod.get("status")).get("phase") != "Running"
                or self._disruption_condition(pod) is not None
            ):
                broken.append(label)
        return broken

    def _delete_jobs(self) -> list[dict[str, Any]]:
        problems: list[dict[str, Any]] = []
        for entry in self.record["jobs"]:
            if entry.get("deleted"):
                continue
            code, _stdout, stderr = self._run(
                "delete",
                "job",
                entry["name"],
                "--namespace",
                entry["namespace"],
                "--ignore-not-found",
                "--wait=false",
            )
            if code == 0:
                entry["deleted"] = True
            else:
                problems.append({**entry, "returncode": code, "stderr": stderr[-_LOG_LIMIT:]})
        self._persist()
        return problems

    # -- the matrix ---------------------------------------------------------

    def _inference_monitor_ip(self) -> tuple[str, str]:
        for item in self._live_pods("gco-system", "app=inference-monitor"):
            metadata = _dict(item.get("metadata"))
            status = _dict(item.get("status"))
            containers = [_dict(entry) for entry in _list(status.get("containerStatuses"))]
            if (
                status.get("phase") == "Running"
                and containers
                and all(entry.get("ready") is True for entry in containers)
                and status.get("podIP")
            ):
                return str(metadata.get("name")), str(status["podIP"])
        raise self._fail("no ready inference-monitor pod to probe")

    @staticmethod
    def _observed(verdict: dict[str, Any]) -> str:
        if verdict["phase"] == "Succeeded" and verdict["exit_code"] == _REACHABLE_EXIT_CODE:
            return "reachable"
        if verdict["phase"] == "Failed" and verdict["exit_code"] == _BLOCKED_EXIT_CODE:
            return "blocked"
        if verdict["phase"] == "Failed" and verdict["exit_code"] == _UNSETTLED_EXIT_CODE:
            return "unsettled"
        return "error"

    @staticmethod
    def _trace(output: str) -> dict[str, Any]:
        """Read the probe script's sampling trace out of its log tail.

        ``samples`` holds every change of answer (``t=0s reachable``,
        ``t=3s blocked``), ``settled`` the closing line, and
        ``attach_window_observed`` is true when the first answer differed from
        the steady state — on the VPC CNI, the standard-mode window between the
        pod's start and its policies being attached.
        """
        samples: list[str] = []
        settled: str | None = None
        for line in output.splitlines():
            if line.startswith(_SAMPLE_PREFIX):
                samples.append(line.removeprefix(_SAMPLE_PREFIX))
            elif line.startswith(_SETTLED_PREFIXES):
                settled = line
        return {
            "samples": samples,
            "settled": settled,
            "attach_window_observed": len(samples) > 1,
        }

    def run(self) -> dict[str, Any]:
        """Start the listeners, run every probe, delete everything, judge the matrix."""
        enforcement = network_policy_enforcement_enabled(self.ctx)
        self.record["enforcement_configured"] = enforcement
        results: list[dict[str, Any]] = []
        evidence: dict[str, Any] = {"enforcement_configured": enforcement, "probes": results}
        try:
            targets: dict[str, dict[str, str]] = {}
            for namespace in ("gco-system", "gco-jobs"):
                name = f"gco-live-netpol-target-{self.token}"
                self._create_job(
                    self._job_manifest(_TARGET_MANIFEST, name=name, namespace=namespace)
                )
                pod_name, pod_ip = self._wait_for_listener(namespace, name)
                targets[namespace] = {"job": name, "pod": pod_name, "ip": pod_ip}
            evidence["targets"] = targets
            monitor_pod, monitor_ip = self._inference_monitor_ip()
            evidence["inference_monitor"] = {"pod": monitor_pod, "ip": monitor_ip}
            self.record["targets"] = targets
            self._persist()

            specs = _probe_specs(targets["gco-system"]["ip"], targets["gco-jobs"]["ip"], monitor_ip)
            launched: list[tuple[ProbeSpec, dict[str, Any]]] = []
            for spec in specs:
                if spec.enforcement_only and not enforcement:
                    results.append(
                        {
                            **asdict(spec),
                            "status": "skipped",
                            "reason": (
                                "eks_cluster.network_policy_enforcement is false in cdk.json; "
                                "no deny verdict is promised"
                            ),
                        }
                    )
                    continue
                job = self._job_manifest(
                    _PROBE_MANIFEST,
                    name=f"gco-live-netpol-{spec.name}-{self.token}",
                    namespace=spec.client_namespace,
                    replacements={"__PROBE_URL__": spec.url},
                )
                self._create_job(job)
                launched.append((spec, job))
            for spec, job in launched:
                verdict, disruptions = self._probe_verdict(spec, job)
                observed = self._observed(verdict)
                results.append(
                    {
                        **asdict(spec),
                        **verdict,
                        **self._trace(verdict["output"]),
                        "job": job["metadata"]["name"],
                        "disruptions": disruptions,
                        "observed": observed,
                        "status": "matched" if observed == spec.expected else "mismatch",
                    }
                )
                self.record["probes"] = results
                self._persist()
            if disrupted := self._disrupted_listeners(targets, monitor_pod):
                self.record["disrupted_listeners"] = disrupted
                raise self._fail(
                    f"listener(s) {', '.join(disrupted)} did not last the probe matrix "
                    "(evicted or replaced), so the verdicts dialed against them are void"
                )
        finally:
            try:
                evidence["cleanup_problems"] = self._delete_jobs()
            except Exception as exc:  # a cleanup error must never mask the verdict
                evidence["cleanup_problems"] = [{"error": f"{type(exc).__name__}: {exc}"}]

        mismatches = [
            f"{item['name']} ({item['client_namespace']} -> {item['url']}) expected "
            f"{item['expected']}, observed {item['observed']} "
            f"[phase={item['phase']} exit={item['exit_code']}]"
            + (
                f" after {len(item['disruptions'])} disruption(s), last: "
                f"{item['disruptions'][-1]['reason']}"
                if item["disruptions"]
                else ""
            )
            for item in results
            if item["status"] == "mismatch"
        ]
        if mismatches:
            raise self._fail("; ".join(mismatches))
        if evidence["cleanup_problems"]:
            raise self._fail(
                f"{len(evidence['cleanup_problems'])} probe Job(s) could not be deleted"
            )
        self.record["evidence"] = evidence
        self._persist()
        return evidence
