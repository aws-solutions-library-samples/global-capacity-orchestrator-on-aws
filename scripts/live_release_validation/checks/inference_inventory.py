"""Kubernetes inventory and stable-absence proof for live inference validation."""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from typing import Any

from .inference_common import ManagedInferenceValidationError


@dataclass(frozen=True)
class KubernetesInventoryKind:
    """One namespaced resource kind that may be owned by an endpoint."""

    summary_key: str
    resource: str
    optional_api: bool = False


KUBERNETES_INVENTORY_KINDS: tuple[KubernetesInventoryKind, ...] = (
    KubernetesInventoryKind("deployments", "deployments.apps"),
    KubernetesInventoryKind("replica_sets", "replicasets.apps"),
    KubernetesInventoryKind("pods", "pods"),
    KubernetesInventoryKind("services", "services"),
    KubernetesInventoryKind("endpoints", "endpoints"),
    KubernetesInventoryKind("endpoint_slices", "endpointslices.discovery.k8s.io"),
    KubernetesInventoryKind("hpas", "horizontalpodautoscalers.autoscaling"),
    KubernetesInventoryKind("scaled_objects", "scaledobjects.keda.sh", optional_api=True),
    KubernetesInventoryKind("config_maps", "configmaps"),
    KubernetesInventoryKind("generated_admin_secrets", "secrets"),
    KubernetesInventoryKind("legacy_ingresses", "ingresses.networking.k8s.io"),
)


class InferenceInventoryMixin:
    """Mixin for bounded kubectl inventory and two-observation absence proof."""

    settings: Any
    kubectl: Any

    def _persist(self) -> None:  # pragma: no cover - implemented by lifecycle
        raise NotImplementedError

    def _record_failure(
        self, record: dict[str, Any], stage: str, exc: BaseException
    ) -> None:  # pragma: no cover - implemented by lifecycle
        raise NotImplementedError

    def _strong_get(
        self, record: dict[str, Any]
    ) -> dict[str, Any] | None:  # pragma: no cover - implemented by lifecycle
        raise NotImplementedError

    @staticmethod
    def _truncated(value: str) -> str:  # pragma: no cover - implemented by lifecycle
        raise NotImplementedError

    @staticmethod
    def _optional_api_missing(stderr: str) -> bool:
        lowered = stderr.casefold()
        return (
            "the server doesn't have a resource type" in lowered
            or "could not find the requested resource" in lowered
        )

    @staticmethod
    def _not_found(stderr: str) -> bool:
        lowered = stderr.casefold()
        return "notfound" in lowered or "not found" in lowered

    def _kubectl_json(
        self,
        record: dict[str, Any],
        *arguments: str,
        optional_api: bool = False,
        deadline: float | None = None,
    ) -> Any | None:
        timeout = float(self.settings.command_timeout_seconds)
        if deadline is not None:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise ManagedInferenceValidationError(
                    "managed inference Kubernetes read exceeded its phase deadline"
                )
            timeout = min(timeout, remaining)
        try:
            code, stdout, stderr = self.kubectl(*arguments, timeout=timeout)
        except Exception as exc:
            self._record_failure(record, "kubectl", exc)
            raise ManagedInferenceValidationError(
                "managed inference Kubernetes read failed; inspect the private checkpoint"
            ) from None
        if code != 0:
            if optional_api and self._optional_api_missing(stderr):
                return {"items": []}
            if self._not_found(stderr):
                return None
            record["last_kubectl_error"] = {
                "argv": list(arguments),
                "returncode": code,
                "stdout": self._truncated(stdout),
                "stderr": self._truncated(stderr),
            }
            self._persist()
            raise ManagedInferenceValidationError(
                "managed inference Kubernetes read failed; inspect the private checkpoint"
            )
        try:
            return json.loads(stdout)
        except json.JSONDecodeError as exc:
            self._record_failure(record, "kubectl-json", exc)
            raise ManagedInferenceValidationError(
                "managed inference Kubernetes read returned invalid JSON"
            ) from None

    @staticmethod
    def _owned_inventory_item(
        summary_key: str,
        item: dict[str, Any],
        endpoint_name: str,
        owned_replica_sets: set[str],
    ) -> bool:
        """Classify one Kubernetes object using exact endpoint ownership."""
        metadata = item.get("metadata") if isinstance(item, dict) else None
        if not isinstance(metadata, dict):
            return False
        name = metadata.get("name")
        labels = metadata.get("labels")
        labels = labels if isinstance(labels, dict) else {}
        deployment_names = {
            endpoint_name,
            f"{endpoint_name}-canary",
            f"{endpoint_name}-prefill",
            f"{endpoint_name}-decode",
            f"{endpoint_name}-proxy",
        }
        service_names = set(deployment_names)
        exact_names: dict[str, set[str]] = {
            "deployments": deployment_names,
            "services": service_names,
            "hpas": {
                endpoint_name,
                f"{endpoint_name}-prefill",
                f"{endpoint_name}-decode",
                f"keda-hpa-{endpoint_name}",
                f"keda-hpa-{endpoint_name}-prefill",
                f"keda-hpa-{endpoint_name}-decode",
            },
            "scaled_objects": {
                endpoint_name,
                f"{endpoint_name}-prefill",
                f"{endpoint_name}-decode",
            },
            "config_maps": {f"{endpoint_name}-mooncake", f"{endpoint_name}-pd-proxy"},
            "generated_admin_secrets": {f"{endpoint_name}-admin"},
            # The Ingresses releases up to v4.1.0 created for an endpoint
            # (the inference monitor's _legacy_ingress_names): the endpoint's
            # own, which its canary shared, and from v3.0.0 the PD proxy's.
            "legacy_ingresses": {
                f"inference-{endpoint_name}",
                f"inference-{endpoint_name}-proxy",
            },
        }
        if summary_key in exact_names:
            return isinstance(name, str) and name in exact_names[summary_key]
        if summary_key == "endpoints":
            return isinstance(name, str) and name in service_names
        if summary_key == "endpoint_slices":
            return labels.get("kubernetes.io/service-name") in service_names
        if summary_key not in {"replica_sets", "pods"}:
            return False

        app_name = labels.get("app")
        if app_name not in deployment_names:
            return False
        if labels.get("project") != "gco" or labels.get("gco.io/type") != "inference":
            return False
        owner_references = metadata.get("ownerReferences")
        owners = owner_references if isinstance(owner_references, list) else []
        if not owners:
            return True
        for owner in owners:
            if not isinstance(owner, dict):
                continue
            owner_kind = owner.get("kind")
            owner_name = owner.get("name")
            if owner_kind == "Deployment" and owner_name in deployment_names:
                return True
            if (
                summary_key == "pods"
                and owner_kind == "ReplicaSet"
                and owner_name in owned_replica_sets
            ):
                return True
        return False

    def kubernetes_inventory(
        self,
        record: dict[str, Any],
        *,
        deadline: float | None = None,
    ) -> dict[str, list[str]]:
        """Inventory endpoint-owned resources using exact deterministic identity."""
        endpoint_name = str(record["name"])
        inventory: dict[str, list[str]] = {}
        owned_replica_sets: set[str] = set()
        for kind in KUBERNETES_INVENTORY_KINDS:
            payload = self._kubectl_json(
                record,
                "get",
                kind.resource,
                "--namespace",
                self.settings.namespace,
                "--output",
                "json",
                optional_api=kind.optional_api,
                deadline=deadline,
            )
            items = payload.get("items", []) if isinstance(payload, dict) else []
            if not isinstance(items, list):
                raise ManagedInferenceValidationError(
                    "managed inference Kubernetes inventory is malformed"
                )
            names: list[str] = []
            for item in items:
                if not isinstance(item, dict) or not self._owned_inventory_item(
                    kind.summary_key,
                    item,
                    endpoint_name,
                    owned_replica_sets,
                ):
                    continue
                metadata = item.get("metadata")
                name = metadata.get("name") if isinstance(metadata, dict) else None
                if isinstance(name, str):
                    names.append(name)
                    if kind.summary_key == "replica_sets":
                        owned_replica_sets.add(name)
            inventory[kind.summary_key] = sorted(set(names))
        record["last_kubernetes_inventory"] = inventory
        self._persist()
        return inventory

    def absence_snapshot(
        self,
        record: dict[str, Any],
        *,
        deadline: float | None = None,
    ) -> tuple[bool, dict[str, Any]]:
        """Read strong DDB state plus the complete Kubernetes ownership inventory."""
        if deadline is not None and time.monotonic() >= deadline:
            raise ManagedInferenceValidationError(
                "managed inference endpoint absence was not proven before timeout"
            )
        item = self._strong_get(record)
        inventory = self.kubernetes_inventory(record, deadline=deadline)
        counts = {key: len(names) for key, names in inventory.items()}
        absent = item is None and not any(counts.values())
        record["last_absence_observation"] = {
            "ddb_present": item is not None,
            "kubernetes": inventory,
        }
        self._persist()
        return absent, {"ddb_absent": item is None, "kubernetes_counts": counts}

    def prove_absence(self, record: dict[str, Any]) -> dict[str, Any]:
        """Require two full absent sweeps separated by one monitor interval."""
        deadline = time.monotonic() + self.settings.deletion_timeout_seconds
        consecutive = 0
        observations = record.setdefault("absence_observations", [])
        if not isinstance(observations, list):
            observations = []
            record["absence_observations"] = observations
        while True:
            absent, evidence = self.absence_snapshot(record, deadline=deadline)
            evidence = {**evidence, "observed_at_monotonic": time.monotonic()}
            observations.append(evidence)
            if len(observations) > 20:
                del observations[:-20]
            consecutive = consecutive + 1 if absent else 0
            record["consecutive_absent_observations"] = consecutive
            self._persist()
            if consecutive >= 2:
                record["absence_proven"] = True
                record["cleanup_phase"] = "absent"
                stable = {
                    **evidence,
                    "stable_absence_observations": consecutive,
                    "separated_by_seconds": self.settings.monitor_interval_seconds,
                }
                record["stable_absence_evidence"] = stable
                self._persist()
                return stable
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise ManagedInferenceValidationError(
                    "managed inference endpoint absence was not proven before timeout"
                )
            delay = (
                self.settings.monitor_interval_seconds
                if absent
                else self.settings.poll_interval_seconds
            )
            time.sleep(min(float(delay), remaining))
