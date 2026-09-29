"""Internal PKI checks: every in-cluster TLS hop chains to the one GCO internal CA.

The in-cluster HTTPS hops (ALB to the API pods, manifest-processor to
cost-monitor, cost-monitor to OpenCost, Prometheus to the service metrics,
the Grafana rotator to Grafana, inference-proxy to the model pods, MLflow's
gco-jobs clients to the tracking server) verify
their peers against a single cert-manager CA: a self-signed bootstrap
``ClusterIssuer`` issues the ``cert-manager/gco-internal-ca`` CA Certificate,
and the ``gco-internal-ca`` ``ClusterIssuer`` signs every leaf from it. A
client that trusts only that CA fails closed, so a leaf that was never
issued, or was issued by anything else, is a broken hop rather than a
cosmetic difference.

On the live cluster this requires, per Region:

* the ``gco-internal-ca`` ClusterIssuer ``Ready``;
* the CA Certificate ``Ready``, ``isCA``, issued by the bootstrap issuer;
* every GCO leaf Certificate the configuration deploys ``Ready`` with
  ``spec.issuerRef`` naming the ``gco-internal-ca`` ClusterIssuer (the
  cost-monitor and OpenCost leaves only with cost monitoring, the Grafana
  leaf and the monitoring trust bundle only with cluster observability, the
  MLflow leaf and the trust-manager bundle source only with MLflow); and
* the retired per-namespace ``gco-api-selfsigned`` Issuer gone.

A missing object, a wrong issuer, or the retired Issuer fails on the first
look; a Certificate cert-manager is still issuing is polled to a deadline.
Reads use the fully qualified ``*.cert-manager.io`` resources so another
controller's ``Certificate`` kind (ACK's ACM controller, for one) can never
answer instead.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any

from ..models import RunContext
from .cluster import KubectlRunner, kubectl_json
from .opencost import _cost_monitoring_configured

INTERNAL_CA_ISSUER = "gco-internal-ca"
BOOTSTRAP_ISSUER = "gco-internal-ca-bootstrap"
CA_CERTIFICATE_NAMESPACE = "cert-manager"
CA_CERTIFICATE_NAME = "gco-internal-ca"
LEGACY_ISSUER_NAMESPACE = "gco-system"
LEGACY_ISSUER_NAME = "gco-api-selfsigned"
_CERTIFICATES = "certificates.cert-manager.io"
_CLUSTER_ISSUERS = "clusterissuers.cert-manager.io"
_ISSUERS = "issuers.cert-manager.io"
#: Issuance takes seconds; this covers a renewal that happens to be in flight.
_READY_TIMEOUT_SECONDS = 300


@dataclass(frozen=True)
class LeafCertificate:
    """One GCO leaf Certificate and the configuration that deploys it."""

    namespace: str
    name: str
    #: ``None`` for always; otherwise the cdk.json feature that gates it.
    feature: str | None = None


LEAF_CERTIFICATES: tuple[LeafCertificate, ...] = (
    LeafCertificate("gco-system", "health-monitor-tls"),
    LeafCertificate("gco-system", "manifest-processor-tls"),
    LeafCertificate("gco-system", "inference-proxy-tls"),
    LeafCertificate("gco-system", "inference-monitor-tls"),
    LeafCertificate("gco-system", "cost-monitor-tls", "cost_monitoring"),
    LeafCertificate("gco-inference", "gco-inference-tls"),
    LeafCertificate("monitoring", "opencost-tls", "cost_monitoring"),
    LeafCertificate("monitoring", "grafana-tls", "cluster_observability"),
    LeafCertificate("monitoring", "gco-monitoring-trust", "cluster_observability"),
    LeafCertificate("monitoring", "mlflow-tls", "mlflow"),
    LeafCertificate("trust-manager", "gco-internal-ca-source", "mlflow"),
)


class InternalPkiValidationError(RuntimeError):
    """The live cluster's certificates do not chain to the GCO internal CA."""


def cluster_observability_configured(ctx: RunContext) -> bool:
    """Return whether cdk.json keeps cluster observability on (default on)."""
    block = ctx.cdk_context.get("cluster_observability")
    if isinstance(block, dict) and "enabled" in block:
        return bool(block["enabled"])
    return True


def mlflow_configured(ctx: RunContext) -> bool:
    """Return whether cdk.json deploys MLflow: its own toggle (default on) and observability."""
    if not cluster_observability_configured(ctx):
        return False
    block = ctx.cdk_context.get("cluster_observability")
    mlflow = block.get("mlflow") if isinstance(block, dict) else None
    if isinstance(mlflow, dict) and "enabled" in mlflow:
        return bool(mlflow["enabled"])
    return True


def expected_leaf_certificates(
    ctx: RunContext,
) -> tuple[list[LeafCertificate], dict[str, str]]:
    """Return the leaves this configuration deploys and the skipped ones with reasons."""
    enabled = {
        "cost_monitoring": _cost_monitoring_configured(ctx),
        "cluster_observability": cluster_observability_configured(ctx),
        "mlflow": mlflow_configured(ctx),
    }
    expected: list[LeafCertificate] = []
    skipped: dict[str, str] = {}
    for leaf in LEAF_CERTIFICATES:
        if leaf.feature is None or enabled[leaf.feature]:
            expected.append(leaf)
        else:
            skipped[f"{leaf.namespace}/{leaf.name}"] = f"{leaf.feature} is disabled in cdk.json"
    return expected, skipped


def _dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _ready(obj: dict[str, Any]) -> bool:
    return any(
        _dict(entry).get("type") == "Ready" and _dict(entry).get("status") == "True"
        for entry in _dict(obj.get("status")).get("conditions") or []
    )


def _issuer_ref(obj: dict[str, Any]) -> dict[str, Any]:
    ref = _dict(_dict(obj.get("spec")).get("issuerRef"))
    return {
        "name": ref.get("name"),
        # cert-manager reads an omitted kind as a namespaced Issuer.
        "kind": ref.get("kind") or "Issuer",
        "group": ref.get("group") or "cert-manager.io",
    }


def _cluster_issuer_ref(ref: dict[str, Any], name: str) -> bool:
    return ref == {"name": name, "kind": "ClusterIssuer", "group": "cert-manager.io"}


def _snapshot(
    ctx: RunContext,
    kubectl: KubectlRunner,
    record: dict[str, Any],
    leaves: list[LeafCertificate],
) -> dict[str, Any]:
    timeout = float(ctx.settings.command_timeout_seconds)
    violations: list[str] = []
    pending: list[str] = []

    issuer = kubectl_json(
        kubectl, record, "get", _CLUSTER_ISSUERS, INTERNAL_CA_ISSUER, timeout=timeout
    )
    issuer_ready = issuer is not None and _ready(issuer)
    if issuer is None:
        violations.append(f"ClusterIssuer {INTERNAL_CA_ISSUER} is missing")
    elif not issuer_ready:
        pending.append(f"ClusterIssuer {INTERNAL_CA_ISSUER} is not Ready")

    ca = kubectl_json(
        kubectl,
        record,
        "get",
        _CERTIFICATES,
        CA_CERTIFICATE_NAME,
        "--namespace",
        CA_CERTIFICATE_NAMESPACE,
        timeout=timeout,
    )
    ca_label = f"Certificate {CA_CERTIFICATE_NAMESPACE}/{CA_CERTIFICATE_NAME}"
    ca_evidence: dict[str, Any] = {"exists": ca is not None}
    if ca is None:
        violations.append(f"{ca_label} is missing")
    else:
        ref = _issuer_ref(ca)
        ca_evidence.update(
            {
                "ready": _ready(ca),
                "is_ca": _dict(ca.get("spec")).get("isCA") is True,
                "issuer": ref["name"],
                "issuer_kind": ref["kind"],
            }
        )
        if not _cluster_issuer_ref(ref, BOOTSTRAP_ISSUER):
            violations.append(f"{ca_label} is not issued by ClusterIssuer {BOOTSTRAP_ISSUER}")
        if not ca_evidence["is_ca"]:
            violations.append(f"{ca_label} is not a CA certificate")
        if not ca_evidence["ready"]:
            pending.append(f"{ca_label} is not Ready")

    certificates: dict[str, Any] = {}
    for leaf in leaves:
        label = f"{leaf.namespace}/{leaf.name}"
        certificate = kubectl_json(
            kubectl,
            record,
            "get",
            _CERTIFICATES,
            leaf.name,
            "--namespace",
            leaf.namespace,
            timeout=timeout,
        )
        if certificate is None:
            certificates[label] = {"exists": False}
            violations.append(f"Certificate {label} is missing")
            continue
        ref = _issuer_ref(certificate)
        ready = _ready(certificate)
        certificates[label] = {
            "exists": True,
            "ready": ready,
            "issuer": ref["name"],
            "issuer_kind": ref["kind"],
        }
        if not _cluster_issuer_ref(ref, INTERNAL_CA_ISSUER):
            violations.append(
                f"Certificate {label} is issued by {ref['kind']} {ref['name']!r}, "
                f"not ClusterIssuer {INTERNAL_CA_ISSUER}"
            )
        elif not ready:
            pending.append(f"Certificate {label} is not Ready")

    legacy = kubectl_json(
        kubectl,
        record,
        "get",
        _ISSUERS,
        LEGACY_ISSUER_NAME,
        "--namespace",
        LEGACY_ISSUER_NAMESPACE,
        timeout=timeout,
    )
    if legacy is not None:
        violations.append(
            f"retired Issuer {LEGACY_ISSUER_NAMESPACE}/{LEGACY_ISSUER_NAME} still exists"
        )
    return {
        "cluster_issuer": {
            "name": INTERNAL_CA_ISSUER,
            "exists": issuer is not None,
            "ready": issuer_ready,
        },
        "ca_certificate": ca_evidence,
        "leaf_certificates": certificates,
        "legacy_issuer_present": legacy is not None,
        "violations": violations,
        "pending": pending,
    }


def verify_internal_pki(ctx: RunContext, region: str, kubectl: KubectlRunner) -> dict[str, Any]:
    """Poll one Region until the internal CA and every expected leaf are Ready.

    Returns the settled snapshot; raises ``InternalPkiValidationError`` on the
    first snapshot with a violation, or when a Certificate is still not Ready
    at the deadline. Every observation is checkpointed under
    ``internal_pki.<region>`` first.
    """
    leaves, skipped = expected_leaf_certificates(ctx)
    with ctx.state_lock:
        record: dict[str, Any] = ctx.checkpoint.state.setdefault("internal_pki", {}).setdefault(
            region, {}
        )
    deadline = time.monotonic() + _READY_TIMEOUT_SECONDS
    while True:
        snapshot = _snapshot(ctx, kubectl, record, leaves)
        snapshot["skipped_certificates"] = skipped
        with ctx.state_lock:
            record["last_snapshot"] = snapshot
        ctx.persist()
        if snapshot["violations"]:
            raise InternalPkiValidationError(
                f"internal PKI in {region} is broken: " + "; ".join(snapshot["violations"])
            )
        if not snapshot["pending"]:
            return snapshot
        if time.monotonic() >= deadline:
            raise InternalPkiValidationError(
                f"internal PKI in {region} did not become Ready within "
                f"{_READY_TIMEOUT_SECONDS}s: " + "; ".join(snapshot["pending"])
            )
        time.sleep(ctx.settings.poll_interval_seconds)
