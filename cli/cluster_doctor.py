"""Diagnosis for the three independent layers of EKS cluster access.

Reaching a GCO cluster's API from a laptop needs three things to be right at
once, and each fails with a misleading symptom:

* **Reachability** — the endpoint mode (PRIVATE needs an SSM tunnel, VPN, or
  bastion; PUBLIC_AND_PRIVATE may carry a CIDR allowlist your egress IP is
  not in). Failure symptom: kubectl timeouts.
* **Authentication** — the cluster authenticates through EKS access entries,
  and by default only platform Lambda roles have one. Failure symptom:
  ``Unauthorized`` even though the network path is fine.
* **Authorization** — an access entry with no associated access policy
  authenticates but can do nothing. Failure symptom: RBAC ``Forbidden``.

Plus the kubeconfig context itself, which is where the two most-confused
failures live: a stale context pointing at a **destroyed** cluster produces
the same kubectl symptom (``no such host``) as a private-only endpoint, and
the two remedies are completely different. ``gco cluster doctor`` names each
layer's state separately and the remedy per case.

The module is split into subprocess probes (thin, monkeypatchable seams that
mirror :mod:`cli.kubectl_helpers`' aws-CLI usage) and a pure
:func:`diagnose` over their results, so the decision table is directly
testable without any AWS access.
"""

from __future__ import annotations

import json
import re
import subprocess
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

from . import kubectl_helpers

_ASSUMED_ROLE_RE = re.compile(r":assumed-role/([^/]+)/")

# Symptom string kubectl prints for a DNS-dead endpoint; used in findings so
# an operator can match what they saw to the diagnosis.
NO_SUCH_HOST = "no such host"


@dataclass(frozen=True)
class DoctorCheck:
    """One layer's diagnosis: what was found and what fixes it."""

    layer: str  # "cluster" | "reachability" | "authentication" | "authorization" | "kubeconfig"
    status: str  # "ok" | "warn" | "fail" | "unknown"
    finding: str
    remedy: str | None = None

    def as_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "layer": self.layer,
            "status": self.status,
            "finding": self.finding,
        }
        if self.remedy:
            payload["remedy"] = self.remedy
        return payload


@dataclass(frozen=True)
class ClusterProbe:
    """Everything the probes could learn about one cluster's access layers."""

    cluster: str
    region: str
    exists: bool
    describe_error: str | None
    endpoint: str
    public: bool
    private: bool
    public_cidrs: list[str]
    caller_arn: str | None
    access_entries: list[str] | None
    associated_policies: list[str] | None
    kubeconfig_server: str | None
    kubeconfig_tunnel_pinned: bool


def _run_aws(args: list[str]) -> subprocess.CompletedProcess[str]:
    """Run one aws-CLI command (list form, never a shell string)."""
    return subprocess.run(  # nosemgrep: dangerous-subprocess-use-audit - fixed argv head, validated inputs, list form, no shell=True
        ["aws", *args],
        capture_output=True,
        text=True,
        check=False,
    )


def caller_principal_arn() -> str | None:
    """The caller's IAM principal, with assumed-role ARNs normalized.

    EKS access entries are created for the base role
    (``arn:...:role/Name``), while STS reports an assumed-role session
    (``arn:...:assumed-role/Name/session``); comparing the raw session ARN
    against the entry list would report a false "no access entry". The same
    normalization ``gco stacks access`` applies when creating the entry.
    """
    try:
        result = _run_aws(["sts", "get-caller-identity", "--output", "json"])
    except FileNotFoundError:
        return None
    if result.returncode != 0:
        return None
    try:
        payload = json.loads(result.stdout or "{}")
    except json.JSONDecodeError:
        return None
    arn = str(payload.get("Arn") or "")
    if not arn:
        return None
    assumed = _ASSUMED_ROLE_RE.search(arn)
    if assumed:
        partition = arn.split(":")[1] if arn.count(":") >= 2 else "aws"
        account = str(payload.get("Account") or "")
        return f"arn:{partition}:iam::{account}:role/{assumed.group(1)}"
    return arn


def list_access_entries(cluster: str, region: str) -> list[str] | None:
    """Principal ARNs holding an EKS access entry, or ``None`` when unknowable."""
    try:
        result = _run_aws(
            [
                "eks",
                "list-access-entries",
                "--cluster-name",
                cluster,
                "--region",
                region,
                "--output",
                "json",
            ]
        )
    except FileNotFoundError:
        return None
    if result.returncode != 0:
        return None
    try:
        payload = json.loads(result.stdout or "{}")
    except json.JSONDecodeError:
        return None
    return [str(entry) for entry in payload.get("accessEntries", [])]


def list_associated_access_policies(
    cluster: str, region: str, principal_arn: str
) -> list[str] | None:
    """Access-policy ARNs associated with one principal, or ``None`` when unknowable."""
    try:
        result = _run_aws(
            [
                "eks",
                "list-associated-access-policies",
                "--cluster-name",
                cluster,
                "--region",
                region,
                "--principal-arn",
                principal_arn,
                "--output",
                "json",
            ]
        )
    except FileNotFoundError:
        return None
    if result.returncode != 0:
        return None
    try:
        payload = json.loads(result.stdout or "{}")
    except json.JSONDecodeError:
        return None
    policies = payload.get("associatedAccessPolicies", [])
    return [
        str(policy.get("policyArn", ""))
        for policy in policies
        if isinstance(policy, dict) and policy.get("policyArn")
    ]


def kubeconfig_cluster_entry(cluster_name: str) -> tuple[str, bool] | None:
    """The kubeconfig ``(server, tunnel_pinned)`` recorded for this cluster.

    Matches entries the same way :func:`cli.kubectl_helpers._tunnel_pinned_server`
    does (exact name or the ARN-shaped ``…cluster/<name>`` suffix), but
    returns the server for ANY matching entry — the doctor needs to see a
    stale non-tunnel server too, not just tunnel pins.
    """
    import yaml

    path = kubectl_helpers._kubeconfig_file()
    try:
        config = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except OSError, yaml.YAMLError:
        return None
    expected_suffix = f"cluster/{cluster_name}"
    for entry in config.get("clusters", []) or []:
        name = str(entry.get("name", ""))
        if name != cluster_name and not name.endswith(expected_suffix):
            continue
        cluster = entry.get("cluster") or {}
        server = str(cluster.get("server", ""))
        host = urlsplit(server).hostname or ""
        pinned = host in kubectl_helpers._LOCAL_TUNNEL_HOSTS and bool(
            cluster.get("tls-server-name")
        )
        return (server, pinned)
    return None


def probe_cluster(cluster: str, region: str) -> ClusterProbe:
    """Collect every layer's raw state for :func:`diagnose`."""
    exists = True
    describe_error: str | None = None
    access: dict[str, Any] = {"endpoint": "", "public": False, "private": False, "public_cidrs": []}
    try:
        access = kubectl_helpers.describe_cluster_access(cluster, region)
    except (RuntimeError, ValueError) as exc:
        exists = False
        describe_error = str(exc)

    caller = caller_principal_arn()
    entries = list_access_entries(cluster, region) if exists else None
    policies: list[str] | None = None
    if exists and caller and entries is not None and caller in entries:
        policies = list_associated_access_policies(cluster, region, caller)

    kubeconfig = kubeconfig_cluster_entry(cluster)
    return ClusterProbe(
        cluster=cluster,
        region=region,
        exists=exists,
        describe_error=describe_error,
        endpoint=str(access.get("endpoint") or ""),
        public=bool(access.get("public")),
        private=bool(access.get("private")),
        public_cidrs=[str(cidr) for cidr in access.get("public_cidrs") or []],
        caller_arn=caller,
        access_entries=entries,
        associated_policies=policies,
        kubeconfig_server=kubeconfig[0] if kubeconfig else None,
        kubeconfig_tunnel_pinned=kubeconfig[1] if kubeconfig else False,
    )


def _host(url: str) -> str:
    return (urlsplit(url).hostname or "").lower()


def _diagnose_missing_cluster(probe: ClusterProbe) -> list[DoctorCheck]:
    """The cluster cannot be described: destroyed, renamed, or unqueryable."""
    checks: list[DoctorCheck] = []
    not_found = "ResourceNotFoundException" in (probe.describe_error or "")
    if not not_found:
        checks.append(
            DoctorCheck(
                layer="cluster",
                status="unknown",
                finding=(
                    f"could not describe cluster {probe.cluster!r} in {probe.region}: "
                    f"{probe.describe_error}"
                ),
                remedy=("Check AWS credentials/region (aws sts get-caller-identity) and retry."),
            )
        )
        return checks

    checks.append(
        DoctorCheck(
            layer="cluster",
            status="fail",
            finding=f"cluster {probe.cluster!r} does not exist in {probe.region}",
            remedy=(
                f"Deploy it (gco stacks deploy {probe.cluster} -y) or check "
                "`gco stacks list` for where this deployment actually runs."
            ),
        )
    )
    if probe.kubeconfig_server and not probe.kubeconfig_tunnel_pinned:
        checks.append(
            DoctorCheck(
                layer="kubeconfig",
                status="fail",
                finding=(
                    f"kubeconfig still has a context for {probe.cluster!r} pointing at "
                    f"{_host(probe.kubeconfig_server)} — kubectl against it fails with "
                    f"'{NO_SUCH_HOST}'. That is a stale context for a destroyed cluster, "
                    "NOT a private-endpoint problem."
                ),
                remedy=(
                    "Remove the stale context (kubectl config delete-context) or refresh "
                    "it after redeploying (aws eks update-kubeconfig "
                    f"--name {probe.cluster} --region {probe.region})."
                ),
            )
        )
    return checks


def _diagnose_reachability(probe: ClusterProbe) -> DoctorCheck:
    if probe.public:
        if probe.public_cidrs and "0.0.0.0/0" not in probe.public_cidrs:
            return DoctorCheck(
                layer="reachability",
                status="ok",
                finding=(
                    f"public endpoint restricted to CIDR allowlist: {', '.join(probe.public_cidrs)}"
                ),
                remedy=(
                    "If kubectl times out from this machine, confirm your egress IP is "
                    "inside the allowlist; adjust with gco stacks eks endpoint set "
                    "PUBLIC_AND_PRIVATE --cidr <your-ip>/32 and redeploy."
                ),
            )
        return DoctorCheck(
            layer="reachability",
            status="ok",
            finding="public endpoint reachable from the internet (0.0.0.0/0; IAM still gates use)",
            remedy=(
                "Consider restricting it: gco stacks eks endpoint set PUBLIC_AND_PRIVATE "
                "--cidr <your-ip>/32, or PRIVATE with gco cluster tunnel."
            ),
        )
    if probe.kubeconfig_tunnel_pinned:
        return DoctorCheck(
            layer="reachability",
            status="ok",
            finding=(
                "PRIVATE endpoint with an SSM tunnel pin in kubeconfig "
                f"({probe.kubeconfig_server}) — kubectl works while that tunnel is open"
            ),
            remedy="If kubectl fails, reopen the tunnel: gco cluster tunnel --via-ssm auto.",
        )
    return DoctorCheck(
        layer="reachability",
        status="warn",
        finding=(
            "PRIVATE endpoint — kubectl from outside the VPC cannot connect "
            "(connection timeouts, NOT an authentication problem)"
        ),
        remedy=(
            "Open a tunnel: gco cluster tunnel --via-ssm auto (self-terminating bastion), "
            "or use a VPN/bastion. Note the tunnel does not replace an access entry."
        ),
    )


def _diagnose_authentication(probe: ClusterProbe) -> DoctorCheck:
    if not probe.caller_arn:
        return DoctorCheck(
            layer="authentication",
            status="unknown",
            finding="could not resolve the caller's IAM principal",
            remedy="Configure AWS credentials (aws sts get-caller-identity must succeed).",
        )
    if probe.access_entries is None:
        return DoctorCheck(
            layer="authentication",
            status="unknown",
            finding="could not list the cluster's EKS access entries",
            remedy=(
                "The caller needs eks:ListAccessEntries to run this check; "
                "try gco stacks access -r " + probe.region + " which also creates the entry."
            ),
        )
    if probe.caller_arn in probe.access_entries:
        return DoctorCheck(
            layer="authentication",
            status="ok",
            finding=f"access entry exists for {probe.caller_arn}",
        )
    return DoctorCheck(
        layer="authentication",
        status="fail",
        finding=(
            f"no EKS access entry for {probe.caller_arn} — kubectl fails with "
            "'Unauthorized' even over a working tunnel or public endpoint"
        ),
        remedy=(
            f"Run gco stacks access -r {probe.region} (one-shot entry + admin policy), "
            "or declare the principal in cdk.json eks_cluster.developer_access and "
            "redeploy for a namespace-scoped grant."
        ),
    )


def _diagnose_authorization(probe: ClusterProbe) -> DoctorCheck | None:
    if not probe.caller_arn or probe.access_entries is None:
        return None
    if probe.caller_arn not in probe.access_entries:
        return None
    if probe.associated_policies is None:
        return DoctorCheck(
            layer="authorization",
            status="unknown",
            finding="could not list associated access policies for the caller's entry",
            remedy="The caller needs eks:ListAssociatedAccessPolicies to run this check.",
        )
    if not probe.associated_policies:
        return DoctorCheck(
            layer="authorization",
            status="fail",
            finding=(
                "the access entry has no associated access policies — kubectl "
                "authenticates but every verb is Forbidden"
            ),
            remedy=(
                f"Run gco stacks access -r {probe.region} to associate "
                "AmazonEKSClusterAdminPolicy, or set a namespace-scoped policy via "
                "cdk.json eks_cluster.developer_access."
            ),
        )
    names = ", ".join(arn.rsplit("/", 1)[-1] for arn in probe.associated_policies)
    return DoctorCheck(
        layer="authorization",
        status="ok",
        finding=f"associated access policies: {names}",
    )


def _diagnose_kubeconfig(probe: ClusterProbe) -> DoctorCheck:
    if probe.kubeconfig_server is None:
        return DoctorCheck(
            layer="kubeconfig",
            status="warn",
            finding=f"no kubeconfig context for {probe.cluster!r}",
            remedy=(
                f"aws eks update-kubeconfig --name {probe.cluster} --region {probe.region} "
                f"(gco stacks access -r {probe.region} also does this)."
            ),
        )
    if probe.kubeconfig_tunnel_pinned:
        return DoctorCheck(
            layer="kubeconfig",
            status="ok",
            finding=f"context pinned to a local SSM tunnel ({probe.kubeconfig_server})",
            remedy="Deliberate tunnel pin; gco commands preserve it while the tunnel is open.",
        )
    if _host(probe.kubeconfig_server) == _host(probe.endpoint):
        return DoctorCheck(
            layer="kubeconfig",
            status="ok",
            finding="context points at the live cluster endpoint",
        )
    return DoctorCheck(
        layer="kubeconfig",
        status="fail",
        finding=(
            f"context points at {_host(probe.kubeconfig_server)} but the live endpoint is "
            f"{_host(probe.endpoint)} — kubectl errors like '{NO_SUCH_HOST}' come from this "
            "stale entry, not from the endpoint access mode"
        ),
        remedy=(
            f"aws eks update-kubeconfig --name {probe.cluster} --region {probe.region} "
            "to repoint the context."
        ),
    )


def diagnose(probe: ClusterProbe) -> list[DoctorCheck]:
    """Turn one probe into per-layer findings with remedies (pure)."""
    if not probe.exists:
        return _diagnose_missing_cluster(probe)
    checks = [
        _diagnose_reachability(probe),
        _diagnose_authentication(probe),
    ]
    authorization = _diagnose_authorization(probe)
    if authorization is not None:
        checks.append(authorization)
    checks.append(_diagnose_kubeconfig(probe))
    return checks


def endpoint_drift(
    configured_mode: str,
    configured_cidrs: list[str],
    live: dict[str, Any],
) -> str | None:
    """Describe cdk.json-vs-live endpoint drift, or ``None`` when converged.

    Used by ``gco stacks status`` so an endpoint flip that was configured
    (``gco stacks eks endpoint set``) but not yet deployed — or applied
    out-of-band and never written back — shows up as explicit drift.
    """
    live_public = bool(live.get("public"))
    live_cidrs = sorted(str(cidr) for cidr in live.get("public_cidrs") or [])
    configured_public = configured_mode == "PUBLIC_AND_PRIVATE"
    if configured_public != live_public:
        live_mode = "PUBLIC_AND_PRIVATE" if live_public else "PRIVATE"
        return (
            f"cdk.json eks_cluster.endpoint_access={configured_mode} but the live "
            f"endpoint is {live_mode}"
        )
    if not configured_public:
        return None
    expected = sorted(str(cidr) for cidr in configured_cidrs) or ["0.0.0.0/0"]
    if expected != live_cidrs:
        return (
            "cdk.json eks_cluster.public_access_cidrs="
            f"[{', '.join(expected)}] but the live allowlist is "
            f"[{', '.join(live_cidrs) or '0.0.0.0/0'}]"
        )
    return None
