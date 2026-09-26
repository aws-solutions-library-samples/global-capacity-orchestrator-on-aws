"""
Tests for the live-validation internal PKI check (``checks/internal_pki.py``).

Covers the configuration-derived leaf set (cost-monitor and OpenCost leaves
only with cost monitoring, the Grafana leaf and the monitoring trust bundle
only with cluster observability), the snapshot read through a scripted
kubectl (the ``gco-internal-ca`` ClusterIssuer, the CA Certificate and its
bootstrap issuer, every leaf's ``spec.issuerRef`` and ``Ready`` condition, and
the retired ``gco-api-selfsigned`` Issuer), the split between breaches that
fail on the first look and issuance that is polled to a deadline, the fully
qualified ``*.cert-manager.io`` resource names, and a cross-check of the names
the check asks for against the shipped certificate manifests. Every kubectl
and clock boundary is faked.
"""

from __future__ import annotations

import json
import re
import threading
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pytest
import yaml

from scripts.live_release_validation.checks import internal_pki as checks
from scripts.live_release_validation.checks.cluster import KubectlError

REGION = "us-east-1"
APPLIER_MANIFESTS = (
    Path(__file__).resolve().parents[1] / "lambda" / "kubectl-applier-simple" / "manifests"
)
_PLACEHOLDER = re.compile(r"\{\{[A-Z0-9_]+\}\}")
ALL_LEAVES = {f"{leaf.namespace}/{leaf.name}" for leaf in checks.LEAF_CERTIFICATES}


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


def _context(*, cdk_context: dict[str, Any] | None = None) -> SimpleNamespace:
    return SimpleNamespace(
        settings=SimpleNamespace(command_timeout_seconds=30, poll_interval_seconds=0),
        checkpoint=SimpleNamespace(state={}),
        state_lock=threading.RLock(),
        cdk_context={} if cdk_context is None else cdk_context,
        persist=MagicMock(),
    )


def _object(
    name: str,
    *,
    ready: bool = True,
    issuer: str | None = checks.INTERNAL_CA_ISSUER,
    kind: str | None = "ClusterIssuer",
    group: str | None = "cert-manager.io",
    is_ca: bool | None = None,
) -> dict[str, Any]:
    spec: dict[str, Any] = {}
    if issuer is not None:
        ref: dict[str, Any] = {"name": issuer}
        if kind is not None:
            ref["kind"] = kind
        if group is not None:
            ref["group"] = group
        spec["issuerRef"] = ref
    if is_ca is not None:
        spec["isCA"] = is_ca
    return {
        "metadata": {"name": name},
        "spec": spec,
        "status": {
            "conditions": [
                "not-a-condition",
                {"type": "Issuing", "status": "False"},
                {"type": "Ready", "status": "True" if ready else "False"},
            ]
        },
    }


class _Cluster:
    """Scripted kubectl answering ``get <resource> <name> [--namespace ns] --output json``."""

    def __init__(self) -> None:
        self.objects: dict[tuple[str, str | None, str], Any] = {}
        self.failures: dict[tuple[str, str | None, str], tuple[int, str, str]] = {}
        self.calls: list[tuple[str, ...]] = []

    def put(self, resource: str, obj: dict[str, Any], namespace: str | None = None) -> None:
        self.objects[(resource, namespace, obj["metadata"]["name"])] = obj

    def __call__(self, *args: str, timeout: float, **kwargs: Any) -> tuple[int, str, str]:
        assert timeout == 30.0
        assert not kwargs
        self.calls.append(args)
        assert args[0] == "get" and args[-2:] == ("--output", "json")
        resource, name = args[1], args[2]
        namespace = args[args.index("--namespace") + 1] if "--namespace" in args else None
        key = (resource, namespace, name)
        if key in self.failures:
            return self.failures[key]
        obj = self.objects.get(key)
        if obj is None:
            return 1, "", f'Error from server (NotFound): {resource} "{name}" not found'
        return 0, json.dumps(obj), ""


def _healthy_cluster() -> _Cluster:
    cluster = _Cluster()
    cluster.put("clusterissuers.cert-manager.io", _object(checks.INTERNAL_CA_ISSUER, issuer=None))
    cluster.put(
        "certificates.cert-manager.io",
        _object(checks.CA_CERTIFICATE_NAME, issuer=checks.BOOTSTRAP_ISSUER, is_ca=True),
        checks.CA_CERTIFICATE_NAMESPACE,
    )
    for leaf in checks.LEAF_CERTIFICATES:
        cluster.put("certificates.cert-manager.io", _object(leaf.name), leaf.namespace)
    return cluster


class TestExpectedLeaves:
    def test_every_leaf_is_expected_with_both_features_on(self) -> None:
        leaves, skipped = checks.expected_leaf_certificates(_context())
        assert leaves == list(checks.LEAF_CERTIFICATES)
        assert skipped == {}

    def test_cost_monitoring_off_drops_the_cost_leaves(self) -> None:
        leaves, skipped = checks.expected_leaf_certificates(
            _context(cdk_context={"cost_monitoring": {"enabled": False}})
        )
        assert {f"{leaf.namespace}/{leaf.name}" for leaf in leaves} == ALL_LEAVES - {
            "gco-system/cost-monitor-tls",
            "monitoring/opencost-tls",
        }
        assert skipped == {
            "gco-system/cost-monitor-tls": "cost_monitoring is disabled in cdk.json",
            "monitoring/opencost-tls": "cost_monitoring is disabled in cdk.json",
        }

    def test_observability_off_drops_the_monitoring_leaves_and_the_cost_pipeline(self) -> None:
        leaves, skipped = checks.expected_leaf_certificates(
            _context(cdk_context={"cluster_observability": {"enabled": False}})
        )
        assert {f"{leaf.namespace}/{leaf.name}" for leaf in leaves} == {
            "gco-system/health-monitor-tls",
            "gco-system/manifest-processor-tls",
            "gco-system/inference-proxy-tls",
            "gco-system/inference-monitor-tls",
            "gco-inference/gco-inference-tls",
        }
        assert skipped["monitoring/grafana-tls"] == (
            "cluster_observability is disabled in cdk.json"
        )
        assert set(skipped) == {
            "gco-system/cost-monitor-tls",
            "monitoring/opencost-tls",
            "monitoring/grafana-tls",
            "monitoring/gco-monitoring-trust",
        }

    @pytest.mark.parametrize(
        ("cdk_context", "expected"),
        [
            ({}, True),
            ({"cluster_observability": "not-a-block"}, True),
            ({"cluster_observability": {"grafana": {}}}, True),
            ({"cluster_observability": {"enabled": False}}, False),
        ],
    )
    def test_cluster_observability_defaults_on(
        self, cdk_context: dict[str, Any], expected: bool
    ) -> None:
        assert checks.cluster_observability_configured(_context(cdk_context=cdk_context)) is (
            expected
        )


class TestSoundPki:
    def test_a_healthy_cluster_returns_the_snapshot_without_waiting(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        clock = _install_clock(monkeypatch)
        ctx = _context()
        cluster = _healthy_cluster()

        snapshot = checks.verify_internal_pki(ctx, REGION, cluster)

        assert snapshot["violations"] == []
        assert snapshot["pending"] == []
        assert snapshot["cluster_issuer"] == {
            "name": "gco-internal-ca",
            "exists": True,
            "ready": True,
        }
        assert snapshot["ca_certificate"] == {
            "exists": True,
            "ready": True,
            "is_ca": True,
            "issuer": "gco-internal-ca-bootstrap",
            "issuer_kind": "ClusterIssuer",
        }
        assert set(snapshot["leaf_certificates"]) == ALL_LEAVES
        assert snapshot["leaf_certificates"]["gco-inference/gco-inference-tls"] == {
            "exists": True,
            "ready": True,
            "issuer": "gco-internal-ca",
            "issuer_kind": "ClusterIssuer",
        }
        assert snapshot["legacy_issuer_present"] is False
        assert snapshot["skipped_certificates"] == {}
        assert clock.sleeps == []
        record = ctx.checkpoint.state["internal_pki"][REGION]
        assert record["last_snapshot"] is snapshot
        ctx.persist.assert_called_once_with()
        # Only the fully qualified cert-manager resources are ever read.
        assert {call[1] for call in cluster.calls} == {
            "clusterissuers.cert-manager.io",
            "certificates.cert-manager.io",
            "issuers.cert-manager.io",
        }
        assert (
            "get",
            "issuers.cert-manager.io",
            "gco-api-selfsigned",
            "--namespace",
            "gco-system",
            "--output",
            "json",
        ) in cluster.calls

    def test_an_issuer_ref_with_the_default_group_is_accepted(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _install_clock(monkeypatch)
        cluster = _healthy_cluster()
        cluster.put(
            "certificates.cert-manager.io",
            _object("health-monitor-tls", group=None),
            "gco-system",
        )
        snapshot = checks.verify_internal_pki(_context(), REGION, cluster)
        assert snapshot["violations"] == []

    def test_disabled_features_are_not_read_and_are_reported_as_skipped(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _install_clock(monkeypatch)
        ctx = _context(cdk_context={"cost_monitoring": {"enabled": False}})
        cluster = _healthy_cluster()

        snapshot = checks.verify_internal_pki(ctx, REGION, cluster)

        assert "gco-system/cost-monitor-tls" not in snapshot["leaf_certificates"]
        assert set(snapshot["skipped_certificates"]) == {
            "gco-system/cost-monitor-tls",
            "monitoring/opencost-tls",
        }
        assert not any("cost-monitor-tls" in call for call in cluster.calls)


class TestBreaches:
    """Breaches fail on the first observation; waiting could not heal them."""

    @staticmethod
    def _failure(cluster: _Cluster, monkeypatch: pytest.MonkeyPatch) -> str:
        clock = _install_clock(monkeypatch)
        ctx = _context()
        with pytest.raises(checks.InternalPkiValidationError) as info:
            checks.verify_internal_pki(ctx, REGION, cluster)
        assert clock.sleeps == []
        assert f"internal PKI in {REGION} is broken" in str(info.value)
        assert ctx.checkpoint.state["internal_pki"][REGION]["last_snapshot"]["violations"]
        return str(info.value)

    def test_missing_cluster_issuer(self, monkeypatch: pytest.MonkeyPatch) -> None:
        cluster = _healthy_cluster()
        del cluster.objects[("clusterissuers.cert-manager.io", None, "gco-internal-ca")]
        assert "ClusterIssuer gco-internal-ca is missing" in self._failure(cluster, monkeypatch)

    def test_missing_ca_certificate(self, monkeypatch: pytest.MonkeyPatch) -> None:
        cluster = _healthy_cluster()
        del cluster.objects[("certificates.cert-manager.io", "cert-manager", "gco-internal-ca")]
        message = self._failure(cluster, monkeypatch)
        assert "Certificate cert-manager/gco-internal-ca is missing" in message

    def test_ca_certificate_from_the_wrong_issuer_or_not_a_ca(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        cluster = _healthy_cluster()
        cluster.put(
            "certificates.cert-manager.io",
            _object("gco-internal-ca", issuer="someone-else", is_ca=False, ready=False),
            "cert-manager",
        )
        message = self._failure(cluster, monkeypatch)
        assert (
            "Certificate cert-manager/gco-internal-ca is not issued by ClusterIssuer "
            "gco-internal-ca-bootstrap"
        ) in message
        assert "Certificate cert-manager/gco-internal-ca is not a CA certificate" in message

    def test_missing_leaf(self, monkeypatch: pytest.MonkeyPatch) -> None:
        cluster = _healthy_cluster()
        del cluster.objects[("certificates.cert-manager.io", "monitoring", "grafana-tls")]
        message = self._failure(cluster, monkeypatch)
        assert "Certificate monitoring/grafana-tls is missing" in message

    @pytest.mark.parametrize(
        ("overrides", "expected"),
        [
            ({"issuer": "gco-api-selfsigned", "kind": "Issuer"}, "Issuer 'gco-api-selfsigned'"),
            # cert-manager reads an omitted kind as a namespaced Issuer.
            ({"kind": None}, "Issuer 'gco-internal-ca'"),
            ({"group": "example.com"}, "ClusterIssuer 'gco-internal-ca'"),
            ({"issuer": None}, "Issuer None"),
        ],
        ids=["legacy-issuer", "default-kind", "foreign-group", "no-issuer-ref"],
    )
    def test_leaf_from_anything_but_the_internal_ca(
        self, monkeypatch: pytest.MonkeyPatch, overrides: dict[str, Any], expected: str
    ) -> None:
        cluster = _healthy_cluster()
        cluster.put(
            "certificates.cert-manager.io",
            _object("inference-proxy-tls", **overrides),
            "gco-system",
        )
        message = self._failure(cluster, monkeypatch)
        assert (
            f"Certificate gco-system/inference-proxy-tls is issued by {expected}, not "
            "ClusterIssuer gco-internal-ca"
        ) in message

    def test_the_retired_issuer_must_be_gone(self, monkeypatch: pytest.MonkeyPatch) -> None:
        cluster = _healthy_cluster()
        cluster.put(
            "issuers.cert-manager.io",
            {"metadata": {"name": "gco-api-selfsigned"}, "spec": {"selfSigned": {}}},
            "gco-system",
        )
        message = self._failure(cluster, monkeypatch)
        assert "retired Issuer gco-system/gco-api-selfsigned still exists" in message

    def test_kubectl_failures_propagate_with_the_recorded_output(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _install_clock(monkeypatch)
        ctx = _context()
        cluster = _healthy_cluster()
        cluster.failures[("clusterissuers.cert-manager.io", None, "gco-internal-ca")] = (
            1,
            "",
            "Error from server (Forbidden): clusterissuers.cert-manager.io is forbidden",
        )
        with pytest.raises(KubectlError):
            checks.verify_internal_pki(ctx, REGION, cluster)
        error = ctx.checkpoint.state["internal_pki"][REGION]["last_kubectl_error"]
        assert error["argv"][:3] == ["get", "clusterissuers.cert-manager.io", "gco-internal-ca"]


class TestIssuanceInFlight:
    def test_not_ready_objects_are_polled_until_they_settle(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        clock = _install_clock(monkeypatch)
        ctx = _context()
        cluster = _healthy_cluster()
        cluster.put(
            "clusterissuers.cert-manager.io", _object("gco-internal-ca", issuer=None, ready=False)
        )
        cluster.put(
            "certificates.cert-manager.io",
            _object("gco-internal-ca", issuer=checks.BOOTSTRAP_ISSUER, is_ca=True, ready=False),
            "cert-manager",
        )
        cluster.put(
            "certificates.cert-manager.io", _object("cost-monitor-tls", ready=False), "gco-system"
        )
        reads = 0

        def issuing(*args: str, **kwargs: Any) -> tuple[int, str, str]:
            # The second full look finds cert-manager done issuing.
            nonlocal reads
            if args[1] == "clusterissuers.cert-manager.io":
                reads += 1
                if reads == 2:
                    for key, obj in list(cluster.objects.items()):
                        cluster.objects[key] = {
                            **obj,
                            "status": {"conditions": [{"type": "Ready", "status": "True"}]},
                        }
            return cluster(*args, **kwargs)

        snapshot = checks.verify_internal_pki(ctx, REGION, issuing)

        assert snapshot["pending"] == []
        assert snapshot["cluster_issuer"]["ready"] is True
        assert clock.sleeps == [0.0]
        assert ctx.persist.call_count == 2

    def test_pending_objects_are_named_at_the_deadline(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        clock = _install_clock(monkeypatch, step=checks._READY_TIMEOUT_SECONDS / 2)
        ctx = _context()
        cluster = _healthy_cluster()
        cluster.put(
            "clusterissuers.cert-manager.io", _object("gco-internal-ca", issuer=None, ready=False)
        )
        cluster.put(
            "certificates.cert-manager.io",
            _object("gco-internal-ca", issuer=checks.BOOTSTRAP_ISSUER, is_ca=True, ready=False),
            "cert-manager",
        )
        cluster.put(
            "certificates.cert-manager.io", _object("opencost-tls", ready=False), "monitoring"
        )

        with pytest.raises(checks.InternalPkiValidationError) as info:
            checks.verify_internal_pki(ctx, REGION, cluster)

        message = str(info.value)
        assert f"did not become Ready within {checks._READY_TIMEOUT_SECONDS}s" in message
        assert "ClusterIssuer gco-internal-ca is not Ready" in message
        assert "Certificate cert-manager/gco-internal-ca is not Ready" in message
        assert "Certificate monitoring/opencost-tls is not Ready" in message
        assert len(clock.sleeps) == 2


class TestShippedManifests:
    """Every name the check asks the cluster for is one the applier ships."""

    @staticmethod
    def _shipped() -> dict[tuple[str, str | None, str], dict[str, Any]]:
        shipped: dict[tuple[str, str | None, str], dict[str, Any]] = {}
        for path in sorted(APPLIER_MANIFESTS.glob("*.yaml")):
            text = _PLACEHOLDER.sub("placeholder", path.read_text(encoding="utf-8"))
            for document in yaml.safe_load_all(text):
                if isinstance(document, dict) and document.get("kind") in {
                    "Certificate",
                    "ClusterIssuer",
                    "Issuer",
                }:
                    metadata = document["metadata"]
                    shipped[(document["kind"], metadata.get("namespace"), metadata["name"])] = (
                        document
                    )
        return shipped

    def test_the_issuer_chain_and_every_leaf_ship_with_the_internal_ca(self) -> None:
        shipped = self._shipped()
        assert ("ClusterIssuer", None, checks.INTERNAL_CA_ISSUER) in shipped
        assert ("ClusterIssuer", None, checks.BOOTSTRAP_ISSUER) in shipped
        ca = shipped[("Certificate", checks.CA_CERTIFICATE_NAMESPACE, checks.CA_CERTIFICATE_NAME)]
        assert ca["spec"]["isCA"] is True
        assert ca["spec"]["issuerRef"]["name"] == checks.BOOTSTRAP_ISSUER
        assert ca["spec"]["issuerRef"]["kind"] == "ClusterIssuer"
        for leaf in checks.LEAF_CERTIFICATES:
            certificate = shipped[("Certificate", leaf.namespace, leaf.name)]
            assert certificate["spec"]["issuerRef"]["name"] == checks.INTERNAL_CA_ISSUER
            assert certificate["spec"]["issuerRef"]["kind"] == "ClusterIssuer"
        assert (
            "Issuer",
            checks.LEGACY_ISSUER_NAMESPACE,
            checks.LEGACY_ISSUER_NAME,
        ) not in shipped
