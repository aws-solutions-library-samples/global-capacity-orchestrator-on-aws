"""Readiness and HPA stability checks for live inference endpoints."""

from __future__ import annotations

import json
import subprocess
import time
from collections.abc import Callable
from typing import Any, cast

from .inference_common import ManagedInferenceValidationError

_TUNNEL_HEARTBEAT_INTERVAL_SECONDS = 240.0

# Workload diagnostics: what the endpoint's pods are doing while a phase waits
# on DDB or Kubernetes. A crash-looping SGLang pod once ran out the whole
# 30-minute readiness window with the checkpoint recording nothing but the
# timeout; the cause (eight restarts, "no kernel image is available for
# execution on the device" on a T4 node) had to be dug out of CloudWatch by
# hand after the cluster was gone. Snapshots are taken every
# _DIAGNOSTICS_INTERVAL_SECONDS during a wait and once more when it fails,
# kept as a bounded ring so a long wait cannot bloat the checkpoint.
_DIAGNOSTICS_INTERVAL_SECONDS = 300.0
_DIAGNOSTICS_RING_SIZE = 8
_DIAGNOSTICS_LOG_TAIL_LINES = 40
_DIAGNOSTICS_LOG_TAIL_BYTES = 6 * 1024
_DIAGNOSTICS_EVENT_LIMIT = 20
_DIAGNOSTICS_NODE_LABELS = (
    "node.kubernetes.io/instance-type",
    "eks.amazonaws.com/instance-family",
    "eks.amazonaws.com/instance-gpu-name",
    "eks.amazonaws.com/instance-gpu-count",
    "eks.amazonaws.com/instance-gpu-memory",
    "karpenter.sh/nodepool",
    "eks.amazonaws.com/nodepool",
    "karpenter.sh/capacity-type",
    "eks.amazonaws.com/capacity-type",
    "topology.kubernetes.io/zone",
)


def _dict_or_empty(value: Any) -> dict[str, Any]:
    """A dict payload as itself, anything else as an empty dict."""
    return value if isinstance(value, dict) else {}


class InferenceRuntimeMixin:
    """Mixin for DDB readiness, Kubernetes readiness, and HPA stability."""

    settings: Any

    def _persist(self) -> None:  # pragma: no cover - implemented by lifecycle
        raise NotImplementedError

    def _strong_get(
        self, record: dict[str, Any]
    ) -> dict[str, Any] | None:  # pragma: no cover - implemented by lifecycle
        raise NotImplementedError

    def _is_owned(
        self, item: dict[str, Any]
    ) -> bool:  # pragma: no cover - implemented by lifecycle
        raise NotImplementedError

    def _verify_item_contract(
        self, plan: Any, item: dict[str, Any], record: dict[str, Any]
    ) -> None:  # pragma: no cover - implemented by lifecycle
        raise NotImplementedError

    def _set_phase(
        self, record: dict[str, Any], phase: str, **values: Any
    ) -> None:  # pragma: no cover - implemented by lifecycle
        raise NotImplementedError

    _kubectl_json: Callable[..., Any | None]
    kubectl: Callable[..., tuple[int, str, str]]

    def keep_cluster_tunnel_alive(
        self,
        record: dict[str, Any],
        last_heartbeat: float,
        *,
        deadline: float | None = None,
    ) -> float:
        """Send bounded Kubernetes traffic before SSM's idle-session timeout."""
        now = time.monotonic()
        if now - last_heartbeat < _TUNNEL_HEARTBEAT_INTERVAL_SECONDS:
            return last_heartbeat
        process_timeout = 8.0
        if deadline is not None:
            remaining = deadline - now
            if remaining <= 0:
                raise ManagedInferenceValidationError(
                    "managed inference tunnel heartbeat deadline expired"
                )
            process_timeout = min(process_timeout, remaining)
        observation: dict[str, Any] = {"started_at_monotonic": now}
        try:
            returncode, stdout, stderr = self.kubectl(
                "--request-timeout=5s",
                "get",
                "--raw=/readyz",
                timeout=process_timeout,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            observation.update(
                {
                    "healthy": False,
                    "error": f"{type(exc).__name__}: {exc}",
                }
            )
        else:
            observation.update(
                {
                    "healthy": returncode == 0 and stdout.strip() == "ok",
                    "returncode": returncode,
                    "stderr": stderr[-1000:],
                }
            )
        history = record.setdefault("tunnel_heartbeats", [])
        if not isinstance(history, list):
            raise ManagedInferenceValidationError("managed tunnel heartbeat history is invalid")
        history.append(observation)
        self._persist()
        if observation["healthy"] is not True:
            raise ManagedInferenceValidationError(
                "managed inference Kubernetes tunnel heartbeat failed"
            )
        return time.monotonic()

    def _wait_for_owned_record(
        self,
        plan: Any,
        record: dict[str, Any],
    ) -> dict[str, Any]:
        """Wait for the run-owned DDB record while keeping the tunnel active."""
        deadline = time.monotonic() + self.settings.readiness_timeout_seconds
        heartbeat_at = float("-inf")
        while True:
            if time.monotonic() >= deadline:
                raise ManagedInferenceValidationError(
                    "managed inference endpoint ownership did not appear before timeout"
                )
            item = self._strong_get(record)
            if item is not None:
                if not self._is_owned(item):
                    raise ManagedInferenceValidationError(
                        "managed inference endpoint collision detected; refusing ownership"
                    )
                self._verify_item_contract(plan, item, record)
                record["owned"] = True
                self._set_phase(record, "ownership-confirmed")
                return item
            heartbeat_at = self.keep_cluster_tunnel_alive(
                record,
                heartbeat_at,
                deadline=deadline,
            )
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise ManagedInferenceValidationError(
                    "managed inference endpoint ownership did not appear before timeout"
                )
            time.sleep(min(float(self.settings.poll_interval_seconds), remaining))

    # ------------------------------------------------------------------
    # Workload diagnostics
    # ------------------------------------------------------------------

    def _diagnostics_read(self, *arguments: str) -> tuple[Any | None, str | None]:
        """One best-effort kubectl read for a diagnostics snapshot.

        Returns ``(payload, error)``; JSON output is decoded, anything else is
        returned as text. Never raises: a snapshot that cannot be taken is
        itself evidence and must not mask the failure being diagnosed.
        """
        timeout = min(float(self.settings.command_timeout_seconds), 30.0)
        try:
            code, stdout, stderr = self.kubectl(*arguments, timeout=timeout)
        except Exception as exc:
            return None, f"{type(exc).__name__}: {exc}"
        if code != 0:
            return None, f"kubectl exited {code}: {stderr.strip()[-500:]}"
        if "json" in arguments:
            try:
                return json.loads(stdout), None
            except json.JSONDecodeError as exc:
                return None, f"kubectl returned non-JSON output: {exc}"
        return stdout, None

    @staticmethod
    def _summarize_container_state(state: Any) -> dict[str, Any] | None:
        """Flatten a container ``state``/``lastState`` block to its one active branch."""
        if not isinstance(state, dict):
            return None
        for branch in ("waiting", "running", "terminated"):
            details = state.get(branch)
            if isinstance(details, dict):
                summary: dict[str, Any] = {"status": branch}
                for key in ("reason", "exitCode", "signal", "startedAt", "finishedAt"):
                    if details.get(key) is not None:
                        summary[key] = details[key]
                message = details.get("message")
                if isinstance(message, str) and message:
                    summary["message"] = message[-500:]
                return summary
        return None

    def _diagnose_pod(self, plan: Any, pod: dict[str, Any]) -> dict[str, Any]:
        """Describe one pod: scheduling, per-container state, and crash output."""
        metadata = _dict_or_empty(pod.get("metadata"))
        spec = _dict_or_empty(pod.get("spec"))
        status = _dict_or_empty(pod.get("status"))
        name = str(metadata.get("name") or "")
        diagnosis: dict[str, Any] = {
            "name": name,
            "phase": status.get("phase"),
            "node": spec.get("nodeName"),
            "start_time": status.get("startTime"),
            "conditions": [],
            "containers": [],
        }
        conditions = status.get("conditions")
        if isinstance(conditions, list):
            for condition in conditions:
                if not isinstance(condition, dict) or condition.get("status") == "True":
                    continue
                condition_entry: dict[str, Any] = {
                    "type": condition.get("type"),
                    "status": condition.get("status"),
                }
                if condition.get("reason"):
                    condition_entry["reason"] = condition["reason"]
                if isinstance(condition.get("message"), str):
                    condition_entry["message"] = condition["message"][-500:]
                diagnosis["conditions"].append(condition_entry)
        statuses = status.get("containerStatuses")
        if isinstance(statuses, list):
            for container_status in statuses:
                if not isinstance(container_status, dict):
                    continue
                container_name = str(container_status.get("name") or "")
                restarts = int(container_status.get("restartCount") or 0)
                ready = container_status.get("ready") is True
                entry: dict[str, Any] = {
                    "name": container_name,
                    "ready": ready,
                    "restart_count": restarts,
                    "state": self._summarize_container_state(container_status.get("state")),
                    "last_state": self._summarize_container_state(
                        container_status.get("lastState")
                    ),
                }
                if name and container_name and (restarts > 0 or not ready):
                    # The current attempt's output, and — the part that names
                    # a crash-loop's cause — the previous attempt's.
                    entry["log_tail"] = self._log_tail(name, container_name, previous=False)
                    if restarts > 0:
                        entry["previous_log_tail"] = self._log_tail(
                            name, container_name, previous=True
                        )
                diagnosis["containers"].append(entry)
        return diagnosis

    def _log_tail(self, pod: str, container: str, *, previous: bool) -> str:
        arguments = [
            "logs",
            pod,
            "--namespace",
            self.settings.namespace,
            "--container",
            container,
            f"--tail={_DIAGNOSTICS_LOG_TAIL_LINES}",
        ]
        if previous:
            arguments.append("--previous")
        payload, error = self._diagnostics_read(*arguments)
        if error is not None:
            return f"<unavailable: {error}>"
        return str(payload)[-_DIAGNOSTICS_LOG_TAIL_BYTES:]

    def _diagnose_events(self, plan: Any) -> list[dict[str, Any]] | str:
        payload, error = self._diagnostics_read(
            "get",
            "events",
            "--namespace",
            self.settings.namespace,
            "--output",
            "json",
        )
        if error is not None:
            return f"<unavailable: {error}>"
        items = payload.get("items") if isinstance(payload, dict) else None
        events: list[dict[str, Any]] = []
        for item in items if isinstance(items, list) else []:
            if not isinstance(item, dict):
                continue
            involved = item.get("involvedObject")
            involved_name = str(involved.get("name") or "") if isinstance(involved, dict) else ""
            if not involved_name.startswith(str(plan.name)):
                continue
            message = item.get("message")
            events.append(
                {
                    "object": (
                        f"{involved.get('kind')}/{involved_name}"
                        if isinstance(involved, dict)
                        else involved_name
                    ),
                    "type": item.get("type"),
                    "reason": item.get("reason"),
                    "count": item.get("count"),
                    "last_timestamp": item.get("lastTimestamp") or item.get("eventTime"),
                    "message": message[-500:] if isinstance(message, str) else message,
                }
            )
        events.sort(key=lambda event: str(event.get("last_timestamp") or ""))
        return events[-_DIAGNOSTICS_EVENT_LIMIT:]

    def _diagnose_node(self, node_name: str) -> dict[str, Any]:
        payload, error = self._diagnostics_read("get", "node", node_name, "--output", "json")
        if error is not None:
            return {"error": error}
        metadata = payload.get("metadata") if isinstance(payload, dict) else None
        labels = metadata.get("labels") if isinstance(metadata, dict) else None
        status = payload.get("status") if isinstance(payload, dict) else None
        allocatable = status.get("allocatable") if isinstance(status, dict) else None
        return {
            "labels": {
                key: labels[key]
                for key in _DIAGNOSTICS_NODE_LABELS
                if isinstance(labels, dict) and key in labels
            },
            "allocatable_gpus": (
                allocatable.get("nvidia.com/gpu") if isinstance(allocatable, dict) else None
            ),
        }

    @staticmethod
    def _diagnostics_summary(snapshot: dict[str, Any]) -> str:
        """One line a human can act on: pods, their worst container state, their GPU."""
        pods = snapshot.get("pods")
        if not isinstance(pods, list) or not pods:
            error = snapshot.get("pods_error")
            return f"no pods observed{f' ({error})' if error else ''}"
        parts: list[str] = []
        nodes = _dict_or_empty(snapshot.get("nodes"))
        for pod in pods:
            phase = pod.get("phase") or "?"
            pieces = [f"{pod.get('name')}: {phase}"]
            for condition in pod.get("conditions") or []:
                if condition.get("type") == "PodScheduled" and condition.get("reason"):
                    pieces.append(f"unscheduled ({condition['reason']})")
            for container in pod.get("containers") or []:
                state = container.get("state") or {}
                last = container.get("last_state") or {}
                detail = state.get("reason") or state.get("status") or "?"
                if last.get("exitCode") is not None:
                    detail += f", last exit {last['exitCode']}"
                    if last.get("reason"):
                        detail += f" {last['reason']}"
                pieces.append(
                    f"{container.get('name')} {detail} (restarts={container.get('restart_count', 0)})"
                )
            node = nodes.get(pod.get("node") or "")
            if isinstance(node, dict) and isinstance(node.get("labels"), dict):
                labels = node["labels"]
                placement = "/".join(
                    str(labels[key])
                    for key in (
                        "node.kubernetes.io/instance-type",
                        "eks.amazonaws.com/instance-gpu-name",
                    )
                    if key in labels
                )
                if placement:
                    pieces.append(f"on {placement}")
            parts.append(", ".join(pieces))
        return "; ".join(parts)

    def capture_workload_diagnostics(
        self,
        plan: Any,
        record: dict[str, Any],
        *,
        reason: str,
    ) -> dict[str, Any]:
        """Snapshot the endpoint's pods, crash output, events and nodes into the record.

        Best-effort and bounded: every read has its own short timeout and a
        failed read is recorded in place of the data. The snapshot ring keeps
        the last ``_DIAGNOSTICS_RING_SIZE`` entries so periodic captures during
        a long wait show the progression (pulling -> running -> crash-looping)
        without growing the checkpoint unboundedly. Returns the snapshot; its
        ``summary`` is what the caller appends to a timeout error.
        """
        snapshot: dict[str, Any] = {
            "reason": reason,
            "captured_at_monotonic": time.monotonic(),
            "pods": [],
            "nodes": {},
        }
        payload, error = self._diagnostics_read(
            "get",
            "pods",
            "--namespace",
            self.settings.namespace,
            "--selector",
            f"app={plan.name}",
            "--output",
            "json",
        )
        if error is not None:
            snapshot["pods_error"] = error
        else:
            items = payload.get("items") if isinstance(payload, dict) else None
            for pod in items if isinstance(items, list) else []:
                if isinstance(pod, dict):
                    snapshot["pods"].append(self._diagnose_pod(plan, pod))
            for node_name in sorted({str(pod["node"]) for pod in snapshot["pods"] if pod["node"]}):
                snapshot["nodes"][node_name] = self._diagnose_node(node_name)
        snapshot["events"] = self._diagnose_events(plan)
        snapshot["summary"] = self._diagnostics_summary(snapshot)

        ring = record.setdefault("workload_diagnostics", [])
        if not isinstance(ring, list):
            ring = []
            record["workload_diagnostics"] = ring
        ring.append(snapshot)
        del ring[:-_DIAGNOSTICS_RING_SIZE]
        record["last_workload_summary"] = snapshot["summary"]
        self._persist()
        return snapshot

    def _diagnostics_due(
        self, record: dict[str, Any], plan: Any, last_capture: float, reason: str
    ) -> float:
        """Take a periodic snapshot when the interval has elapsed; return the new mark."""
        now = time.monotonic()
        if now - last_capture < _DIAGNOSTICS_INTERVAL_SECONDS:
            return last_capture
        self.capture_workload_diagnostics(plan, record, reason=reason)
        return now

    def _timeout_error(
        self, plan: Any, record: dict[str, Any], message: str, *, reason: str
    ) -> ManagedInferenceValidationError:
        """Build a timeout error that carries the final workload diagnosis."""
        snapshot = self.capture_workload_diagnostics(plan, record, reason=reason)
        return ManagedInferenceValidationError(f"{message} ({snapshot['summary']})")

    def wait_for_ddb_running(self, plan: Any, record: dict[str, Any]) -> None:
        """Require this run's exact DDB record and running regional observation."""
        deadline = time.monotonic() + self.settings.readiness_timeout_seconds
        heartbeat_at = float("-inf")
        diagnostics_at = time.monotonic()
        while True:
            if time.monotonic() >= deadline:
                raise self._timeout_error(
                    plan,
                    record,
                    "managed inference DDB running state was not observed before timeout",
                    reason="ddb-running-timeout",
                )
            item = self._strong_get(record)
            if item is not None:
                if not self._is_owned(item):
                    raise ManagedInferenceValidationError(
                        "managed inference endpoint ownership changed while waiting"
                    )
                self._verify_item_contract(plan, item, record)
                statuses = item.get("region_status")
                regional = (
                    statuses.get(self.settings.selected_region)
                    if isinstance(statuses, dict)
                    else None
                )
                # The monitor's own view of the region — its state and any
                # message it wrote — is the cheapest evidence there is.
                record["last_ddb_observation"] = {
                    "desired_state": item.get("desired_state"),
                    "regional": regional if isinstance(regional, dict) else None,
                    "observed_at_monotonic": time.monotonic(),
                }
                if (
                    item.get("desired_state") == "running"
                    and isinstance(regional, dict)
                    and regional.get("state") == "running"
                ):
                    self._set_phase(record, "ddb-running")
                    return
            heartbeat_at = self.keep_cluster_tunnel_alive(
                record,
                heartbeat_at,
                deadline=deadline,
            )
            diagnostics_at = self._diagnostics_due(record, plan, diagnostics_at, "ddb-running-wait")
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise self._timeout_error(
                    plan,
                    record,
                    "managed inference DDB running state was not observed before timeout",
                    reason="ddb-running-timeout",
                )
            time.sleep(min(float(self.settings.poll_interval_seconds), remaining))

    @staticmethod
    def _ready_condition(container_statuses: Any) -> bool:
        return (
            isinstance(container_statuses, list)
            and bool(container_statuses)
            and all(
                isinstance(status, dict) and status.get("ready") is True
                for status in container_statuses
            )
        )

    def _deployment_ready_snapshot(
        self,
        plan: Any,
        record: dict[str, Any],
        expected_replicas: int,
        *,
        exact: bool,
        deadline: float | None = None,
    ) -> tuple[bool, dict[str, int]]:
        deployment = self._kubectl_json(
            record,
            "get",
            "deployment",
            plan.name,
            "--namespace",
            self.settings.namespace,
            "--output",
            "json",
            deadline=deadline,
        )
        if not isinstance(deployment, dict):
            return False, {}
        metadata = deployment.get("metadata")
        spec = deployment.get("spec")
        status = deployment.get("status")
        if (
            not isinstance(metadata, dict)
            or not isinstance(spec, dict)
            or not isinstance(status, dict)
        ):
            return False, {}
        desired = int(spec.get("replicas") or 0)
        ready = int(status.get("readyReplicas") or 0)
        available = int(status.get("availableReplicas") or 0)
        updated = int(status.get("updatedReplicas") or 0)
        generation = int(metadata.get("generation") or 0)
        observed = int(status.get("observedGeneration") or 0)
        replica_match = desired == expected_replicas if exact else desired >= expected_replicas
        deployment_ready = (
            replica_match
            and ready >= expected_replicas
            and available >= expected_replicas
            and updated >= expected_replicas
            and observed >= generation
        )

        pods_payload = self._kubectl_json(
            record,
            "get",
            "pods",
            "--namespace",
            self.settings.namespace,
            "--selector",
            f"app={plan.name}",
            "--output",
            "json",
            deadline=deadline,
        )
        items = pods_payload.get("items", []) if isinstance(pods_payload, dict) else []
        ready_pods = 0
        if isinstance(items, list):
            for item in items:
                pod_status = item.get("status") if isinstance(item, dict) else None
                if (
                    isinstance(pod_status, dict)
                    and pod_status.get("phase") == "Running"
                    and self._ready_condition(pod_status.get("containerStatuses"))
                ):
                    ready_pods += 1
        evidence = {
            "desired": desired,
            "ready": ready,
            "available": available,
            "updated": updated,
            "ready_pods": ready_pods,
        }
        return deployment_ready and ready_pods >= expected_replicas, evidence

    def wait_for_kubernetes_ready(self, plan: Any, record: dict[str, Any]) -> None:
        """Require Deployment convergence and ready Running pods."""
        deadline = time.monotonic() + self.settings.readiness_timeout_seconds
        diagnostics_at = time.monotonic()
        while True:
            ready, evidence = self._deployment_ready_snapshot(
                plan,
                record,
                plan.replicas,
                exact=not plan.autoscaling,
                deadline=deadline,
            )
            record["last_readiness"] = evidence
            self._persist()
            if ready:
                self._set_phase(record, "kubernetes-ready")
                return
            if time.monotonic() >= deadline:
                raise self._timeout_error(
                    plan,
                    record,
                    "managed inference Kubernetes readiness was not observed before timeout",
                    reason="kubernetes-ready-timeout",
                )
            diagnostics_at = self._diagnostics_due(
                record, plan, diagnostics_at, "kubernetes-ready-wait"
            )
            time.sleep(
                min(
                    float(self.settings.poll_interval_seconds),
                    max(0.0, deadline - time.monotonic()),
                )
            )

    def verify_no_container_restarts(self, plan: Any, record: dict[str, Any]) -> None:
        """Refuse a leg whose containers restarted on the way to serving.

        Readiness, HPA stability and a successful invocation can all be
        observed *between* restarts: an SGLang container whose probes timed
        out was killed by liveness every ~3 minutes while readiness flapped
        true often enough for the leg to pass (2026-09-18, A10G). A restart
        means a crash or a probe kill, and either is a defect the release
        must not carry, so the final diagnostics snapshot is audited: every
        container of every pod must report ``restartCount`` 0. The snapshot
        already holds the previous attempt's log tail and the pod events
        (``Liveness probe failed``, ``Killing``), so the failure names its
        cause. A snapshot that could not list the pods fails closed.
        """
        snapshot = self.capture_workload_diagnostics(plan, record, reason="restart-audit")
        if "pods_error" in snapshot:
            raise ManagedInferenceValidationError(
                "managed inference restart audit could not list the endpoint's pods "
                f"({snapshot['pods_error']})"
            )
        pods = snapshot["pods"]
        if not pods:
            raise ManagedInferenceValidationError(
                "managed inference restart audit found no pods for a serving endpoint"
            )
        restarted = [
            f"{pod.get('name')}/{container.get('name')} restarted "
            f"{int(container.get('restart_count') or 0)}x"
            for pod in pods
            for container in pod.get("containers") or []
            if int(container.get("restart_count") or 0) > 0
        ]
        record["restart_audit"] = {
            "pods": [str(pod.get("name")) for pod in pods],
            "restarted": restarted,
        }
        self._persist()
        if restarted:
            raise ManagedInferenceValidationError(
                "managed inference containers restarted during the leg: "
                f"{'; '.join(restarted)} ({snapshot['summary']})"
            )
        self._set_phase(record, "restart-audited")

    def _hpa_matches(
        self,
        plan: Any,
        record: dict[str, Any],
        *,
        deadline: float | None = None,
    ) -> bool:
        hpa = self._kubectl_json(
            record,
            "get",
            "horizontalpodautoscaler.autoscaling",
            plan.name,
            "--namespace",
            self.settings.namespace,
            "--output",
            "json",
            deadline=deadline,
        )
        if not isinstance(hpa, dict):
            return False
        spec = hpa.get("spec")
        if not isinstance(spec, dict):
            return False
        target = spec.get("scaleTargetRef")
        if not isinstance(target, dict) or target != {
            "apiVersion": "apps/v1",
            "kind": "Deployment",
            "name": plan.name,
        }:
            return False
        if spec.get("minReplicas") != self.settings.hpa_min_replicas:
            return False
        if spec.get("maxReplicas") != self.settings.hpa_max_replicas:
            return False
        metrics = spec.get("metrics")
        if not isinstance(metrics, list):
            return False
        return any(
            isinstance(metric, dict)
            and metric.get("type") == "Resource"
            and isinstance(metric.get("resource"), dict)
            and metric["resource"].get("name") == "cpu"
            and isinstance(metric["resource"].get("target"), dict)
            and metric["resource"]["target"].get("type") == "Utilization"
            and metric["resource"]["target"].get("averageUtilization")
            == self.settings.hpa_cpu_target
            for metric in metrics
        )

    def verify_shared_proxy_autoscaling(self, state: dict[str, Any]) -> None:
        """Prove the deployed TLS sidecar request and active ContainerResource HPA."""
        record = state.setdefault(
            "shared_proxy_autoscaling",
            {
                "namespace": "gco-system",
                "deployment": "inference-proxy",
                "hpa": "inference-proxy-hpa",
                "phase": "waiting",
                "commands": [],
            },
        )
        if not isinstance(record, dict):
            raise ManagedInferenceValidationError("shared proxy checkpoint evidence is invalid")
        record["phase"] = "waiting"
        self._persist()
        deadline = time.monotonic() + self.settings.hpa_timeout_seconds
        while True:
            deployment = self._kubectl_json(
                record,
                "get",
                "deployment",
                "inference-proxy",
                "--namespace",
                "gco-system",
                "--output",
                "json",
                deadline=deadline,
            )
            hpa = self._kubectl_json(
                record,
                "get",
                "horizontalpodautoscaler.autoscaling",
                "inference-proxy-hpa",
                "--namespace",
                "gco-system",
                "--output",
                "json",
                deadline=deadline,
            )
            observed: dict[str, Any] = {}
            if isinstance(deployment, dict):
                deployment_spec = deployment.get("spec")
                template = (
                    deployment_spec.get("template") if isinstance(deployment_spec, dict) else None
                )
                pod_spec = template.get("spec") if isinstance(template, dict) else None
                containers = pod_spec.get("containers") if isinstance(pod_spec, dict) else None
                tls_containers = (
                    [
                        item
                        for item in containers
                        if isinstance(item, dict) and item.get("name") == "api-tls-proxy"
                    ]
                    if isinstance(containers, list)
                    else []
                )
                if len(tls_containers) == 1:
                    resources = tls_containers[0].get("resources")
                    requests = resources.get("requests") if isinstance(resources, dict) else None
                    if isinstance(requests, dict):
                        observed["tls_cpu_request"] = requests.get("cpu")
            if isinstance(hpa, dict):
                metadata_value = hpa.get("metadata")
                spec_value = hpa.get("spec")
                status_value = hpa.get("status")
                metadata = (
                    cast(dict[str, Any], metadata_value) if isinstance(metadata_value, dict) else {}
                )
                spec = cast(dict[str, Any], spec_value) if isinstance(spec_value, dict) else {}
                status = (
                    cast(dict[str, Any], status_value) if isinstance(status_value, dict) else {}
                )
                target = spec.get("scaleTargetRef")
                metrics = spec.get("metrics")

                def matching_tls_metric(metric: object, *, current: bool) -> bool:
                    if not isinstance(metric, dict) or metric.get("type") != "ContainerResource":
                        return False
                    source = metric.get("containerResource")
                    if not isinstance(source, dict):
                        return False
                    value = source.get("current" if current else "target")
                    return (
                        source.get("name") == "cpu"
                        and source.get("container") == "api-tls-proxy"
                        and isinstance(value, dict)
                        and (current or value.get("type") == "Utilization")
                    )

                tls_metrics = (
                    [metric for metric in metrics if matching_tls_metric(metric, current=False)]
                    if isinstance(metrics, list)
                    else []
                )
                current_metrics = status.get("currentMetrics")
                active_metrics = (
                    [
                        metric
                        for metric in current_metrics
                        if matching_tls_metric(metric, current=True)
                    ]
                    if isinstance(current_metrics, list)
                    else []
                )
                conditions = status.get("conditions")
                active_conditions = (
                    [
                        condition
                        for condition in conditions
                        if isinstance(condition, dict)
                        and condition.get("type") == "ScalingActive"
                        and condition.get("status") == "True"
                    ]
                    if isinstance(conditions, list)
                    else []
                )
                tls_target: object = None
                if len(tls_metrics) == 1:
                    # matching_tls_metric admitted this metric only after proving
                    # containerResource.target is a Utilization object.
                    tls_target = tls_metrics[0]["containerResource"]["target"].get(
                        "averageUtilization"
                    )
                observed.update(
                    {
                        "target_matches": target
                        == {
                            "apiVersion": "apps/v1",
                            "kind": "Deployment",
                            "name": "inference-proxy",
                        },
                        "tls_metric_count": len(tls_metrics),
                        "tls_cpu_target": tls_target,
                        "active_tls_metric_count": len(active_metrics),
                        "scaling_active": bool(active_conditions),
                        "scaling_active_reason": (
                            active_conditions[0].get("reason") if active_conditions else None
                        ),
                        "observed_generation_current": int(status.get("observedGeneration") or 0)
                        >= int(metadata.get("generation") or 0),
                    }
                )
            record["last_observed"] = observed
            self._persist()
            if (
                observed.get("tls_cpu_request") == self.settings.proxy_tls_cpu_request
                and observed.get("target_matches") is True
                and observed.get("tls_metric_count") == 1
                and observed.get("tls_cpu_target") == self.settings.proxy_tls_cpu_target
                and observed.get("active_tls_metric_count") == 1
                and observed.get("scaling_active") is True
                and observed.get("observed_generation_current") is True
            ):
                record["phase"] = "verified"
                record["expected"] = {
                    "tls_cpu_request": self.settings.proxy_tls_cpu_request,
                    "tls_cpu_target": self.settings.proxy_tls_cpu_target,
                }
                self._persist()
                return
            if time.monotonic() >= deadline:
                raise ManagedInferenceValidationError(
                    "shared inference-proxy TLS autoscaling contract was not active before timeout"
                )
            time.sleep(
                min(
                    float(self.settings.poll_interval_seconds),
                    max(0.0, deadline - time.monotonic()),
                )
            )

    def verify_hpa_stability(self, plan: Any, record: dict[str, Any]) -> None:
        """Prove HPA target/bounds and two full monitor intervals at two replicas."""
        deadline = time.monotonic() + self.settings.hpa_timeout_seconds
        while not self._hpa_matches(plan, record, deadline=deadline):
            if time.monotonic() >= deadline:
                raise ManagedInferenceValidationError(
                    "managed inference HPA contract was not observed before timeout"
                )
            time.sleep(
                min(
                    float(self.settings.poll_interval_seconds),
                    max(0.0, deadline - time.monotonic()),
                )
            )
        self._set_phase(record, "hpa-verified")

        while True:
            ready, evidence = self._deployment_ready_snapshot(
                plan,
                record,
                self.settings.hpa_min_replicas,
                exact=True,
                deadline=deadline,
            )
            record["last_hpa_replica_observation"] = evidence
            self._persist()
            if ready:
                break
            if time.monotonic() >= deadline:
                raise ManagedInferenceValidationError(
                    "managed inference HPA did not reach two ready replicas before timeout"
                )
            time.sleep(
                min(
                    float(self.settings.poll_interval_seconds),
                    max(0.0, deadline - time.monotonic()),
                )
            )

        observations = [record["last_hpa_replica_observation"]]
        monitor_interval = float(self.settings.monitor_interval_seconds)
        for _ in range(self.settings.hpa_stability_intervals):
            if deadline - time.monotonic() < monitor_interval:
                raise ManagedInferenceValidationError(
                    "managed inference HPA stability exceeded its phase deadline"
                )
            time.sleep(monitor_interval)
            ready, evidence = self._deployment_ready_snapshot(
                plan,
                record,
                self.settings.hpa_min_replicas,
                exact=True,
                deadline=deadline,
            )
            observations.append(evidence)
            if not ready:
                record["hpa_stability_observations"] = observations
                self._persist()
                raise ManagedInferenceValidationError(
                    "managed inference HPA replicas did not remain stable"
                )
        record["hpa_stability_observations"] = observations
        self._set_phase(record, "hpa-stable")
