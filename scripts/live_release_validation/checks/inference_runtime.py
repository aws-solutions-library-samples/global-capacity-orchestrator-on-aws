"""Readiness and HPA stability checks for live inference endpoints."""

from __future__ import annotations

import subprocess
import time
from collections.abc import Callable
from typing import Any, cast

from .inference_common import ManagedInferenceValidationError

_TUNNEL_HEARTBEAT_INTERVAL_SECONDS = 240.0


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

    def wait_for_ddb_running(self, plan: Any, record: dict[str, Any]) -> None:
        """Require this run's exact DDB record and running regional observation."""
        deadline = time.monotonic() + self.settings.readiness_timeout_seconds
        heartbeat_at = float("-inf")
        while True:
            if time.monotonic() >= deadline:
                raise ManagedInferenceValidationError(
                    "managed inference DDB running state was not observed before timeout"
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
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise ManagedInferenceValidationError(
                    "managed inference DDB running state was not observed before timeout"
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
                raise ManagedInferenceValidationError(
                    "managed inference Kubernetes readiness was not observed before timeout"
                )
            time.sleep(
                min(
                    float(self.settings.poll_interval_seconds),
                    max(0.0, deadline - time.monotonic()),
                )
            )

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
