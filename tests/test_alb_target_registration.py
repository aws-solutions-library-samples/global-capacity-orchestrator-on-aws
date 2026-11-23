"""Rollouts behind the platform ALB wait until the new pods are healthy targets.

The health monitor, manifest processor and inference proxy sit behind the
internal ALB the AWS Load Balancer Controller builds from
``post-helm-gateway.yaml``. A redeploy once left the health monitor's target
group without a healthy target for nine minutes: the controller's chart
generated a new webhook certificate on the Helm upgrade, the
TargetGroupBinding updates the controller makes through its own webhook
failed until the kubelet refreshed the mounted Secret, and the retries backed
off for about 12 minutes while the rollout went on to replace every old pod.

These tests pin the three settings that close that path:

* the chart reuses the webhook's TLS Secret on upgrades (``keepTLSSecret``);
* a failed TargetGroupBinding reconcile is retried at least once a minute;
* ``gco-system`` carries the readiness-gate label, every ALB backend lives
  there and rolls with ``maxUnavailable: 0``, so the controller's
  target-health gate holds each old pod until its successor is a healthy
  target; the pod mutator fails open, so a controller outage never blocks
  pod creation in ``gco-system``.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parent.parent
MANIFESTS = ROOT / "lambda" / "kubectl-applier-simple" / "manifests"
CHARTS = ROOT / "lambda" / "helm-installer" / "charts.yaml"
READINESS_GATE_LABEL = "elbv2.k8s.aws/pod-readiness-gate-inject"


def _documents(filename: str) -> list[dict[str, Any]]:
    """A manifest's documents, with ``{{PLACEHOLDER}}`` tokens made parseable."""
    text = re.sub(r"\{\{[A-Z0-9_]+\}\}", "1", (MANIFESTS / filename).read_text(encoding="utf-8"))
    return [doc for doc in yaml.safe_load_all(text) if isinstance(doc, dict)]


def _all_documents() -> list[dict[str, Any]]:
    return [doc for path in sorted(MANIFESTS.glob("*.yaml")) for doc in _documents(path.name)]


def _lbc_values() -> dict[str, Any]:
    charts = yaml.safe_load(CHARTS.read_text(encoding="utf-8"))["charts"]
    values: dict[str, Any] = charts["aws-load-balancer-controller"]["values"]
    return values


def _alb_backends() -> set[tuple[str, str]]:
    """(namespace, Service name) of every HTTPRoute and TargetGroupConfiguration backend."""
    backends: set[tuple[str, str]] = set()
    for doc in _documents("post-helm-gateway.yaml"):
        namespace = doc["metadata"].get("namespace")
        if doc["kind"] == "HTTPRoute":
            for rule in doc["spec"]["rules"]:
                for ref in rule.get("backendRefs", []):
                    assert ref.get("kind", "Service") == "Service"
                    backends.add((ref.get("namespace", namespace), ref["name"]))
        if doc["kind"] == "TargetGroupConfiguration" and "targetReference" in doc["spec"]:
            backends.add((namespace, doc["spec"]["targetReference"]["name"]))
    return backends


class TestLoadBalancerControllerValues:
    def test_the_webhook_certificate_survives_chart_upgrades(self) -> None:
        values = _lbc_values()
        assert values["keepTLSSecret"] is True
        # keepTLSSecret has no effect with cert-manager, which GCO does not use here.
        assert not values.get("enableCertManager", False)

    def test_failed_target_group_binding_reconciles_retry_within_a_minute(self) -> None:
        assert _lbc_values()["targetgroupbindingMaxExponentialBackoffDelay"] == "60s"

    def test_the_pod_mutator_fails_open(self) -> None:
        """A controller outage must not stop gco-system pods from being created."""
        assert _lbc_values()["podMutatorWebhookConfig"] == {"failurePolicy": "Ignore"}

    def test_readiness_gate_injection_is_not_switched_off(self) -> None:
        assert _lbc_values().get("enablePodReadinessGateInject", True) is True


class TestReadinessGates:
    def test_only_gco_system_asks_for_readiness_gates(self) -> None:
        labels = {
            doc["metadata"]["name"]: doc["metadata"].get("labels", {})
            for doc in _documents("00-namespaces.yaml")
            if doc["kind"] == "Namespace"
        }
        assert labels["gco-system"][READINESS_GATE_LABEL] == "enabled"
        assert READINESS_GATE_LABEL not in labels["gco-jobs"]
        assert READINESS_GATE_LABEL not in labels["gco-inference"]

    def test_every_alb_backend_is_in_the_labelled_namespace(self) -> None:
        backends = _alb_backends()
        assert backends == {
            ("gco-system", "health-monitor"),
            ("gco-system", "manifest-processor"),
            ("gco-system", "inference-proxy"),
        }

    def test_every_alb_backend_rolls_without_losing_a_ready_pod(self) -> None:
        """The gate holds an old pod only if the rollout may not take it down first."""
        documents = _all_documents()
        services = {
            (doc["metadata"]["namespace"], doc["metadata"]["name"]): doc
            for doc in documents
            if doc["kind"] == "Service" and "namespace" in doc["metadata"]
        }
        deployments = [doc for doc in documents if doc["kind"] == "Deployment"]
        for namespace, name in _alb_backends():
            selector = services[(namespace, name)]["spec"]["selector"]
            (deployment,) = [
                doc
                for doc in deployments
                if doc["metadata"]["namespace"] == namespace
                and selector.items() <= doc["spec"]["template"]["metadata"]["labels"].items()
            ]
            strategy = deployment["spec"]["strategy"]
            assert strategy["type"] == "RollingUpdate", name
            assert strategy["rollingUpdate"]["maxUnavailable"] == 0, name
            assert strategy["rollingUpdate"]["maxSurge"] >= 1, name
