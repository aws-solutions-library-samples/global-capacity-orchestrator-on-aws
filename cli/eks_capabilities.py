"""Configured-vs-live view of the EKS Capabilities on a GCO regional cluster.

Backs ``gco stacks capabilities status`` and the ``eks_capabilities_status`` MCP
tool. EKS Capabilities are AWS-managed Argo CD, ACK and kro installations that
GCO attaches per cluster from the opt-in ``cdk.json`` ``eks_capabilities``
block (see ``docs/EKS_CAPABILITIES.md``). This module answers the operator's
question in one document: what does cdk.json say should be attached here, what
is actually attached (``ListCapabilities`` / ``DescribeCapability``), is each
one ``ACTIVE``, and where do the two disagree.

Deliberately AWS-API only: no kubectl, so it works from any host with AWS
credentials, including against a private-endpoint cluster. The Argo CD GitOps
hand-off's in-cluster sync/health state is the live-validation harness's job
(``scripts/live_release_validation``), not this command's.

Pure functions (``build_status``) are separated from the AWS calls
(``describe_live_capabilities``) so the merge logic is unit-testable without a
fake EKS endpoint. Imports nothing from ``aws_cdk``: the CLI runs without the
CDK toolchain installed.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping
from datetime import datetime
from pathlib import Path
from typing import Any

from gco.eks_capabilities_config import (
    CAPABILITY_TYPE_API_NAMES,
    EKS_CAPABILITIES_CONTEXT_KEY,
    EKS_CAPABILITY_TYPES,
    GITOPS_CODECOMMIT_DEFAULT_BRANCH,
    GITOPS_PROJECT_NAME,
    GITOPS_ROOT_APPLICATION_NAME,
    aws_url_suffix_for_region,
    capability_enabled_in_region,
    effective_gitops_path,
    gitops_codecommit_repository_name,
    gitops_enabled_in_region,
    gitops_repository_url,
    gitops_source,
    merge_eks_capabilities_overrides,
    parse_eks_capabilities_overrides,
    render_gitops_path,
    validate_eks_capabilities_config,
)

#: The only capability status that means "attached and serving".
ACTIVE_STATUS = "ACTIVE"

#: ``AWS::EKS::Capability`` ``Type`` value -> cdk.json spelling.
_API_TYPE_TO_CONFIG: dict[str, str] = {
    api_name: type_name for type_name, api_name in CAPABILITY_TYPE_API_NAMES.items()
}


def capability_name(project_name: str, type_name: str) -> str:
    """The deterministic name the regional stack gives each capability."""
    return f"{project_name}-{type_name}"


def cluster_name_for(project_name: str, region: str) -> str:
    """The regional cluster's name (``<project>-<region>``)."""
    return f"{project_name}-{region}"


def load_eks_capabilities_config(
    cdk_json_path: Path | None = None,
    *,
    overrides: object = None,
) -> dict[str, Any]:
    """The normalized ``eks_capabilities`` block from cdk.json.

    Validated with the same rules the CDK app applies (including the per-type
    ``regions`` subsets against ``deployment_regions.regional``), so a block
    the next deploy would reject is reported here instead of being rendered as
    if it were live intent. ``overrides`` is the run-scoped
    ``eks_capabilities_overrides`` value (a JSON object string or mapping) a
    harness deployed with, merged in exactly as the CDK app merges it. Raises
    ``RuntimeError`` when cdk.json is missing and
    ``gco.eks_capabilities_config.EksCapabilitiesConfigError`` (a
    ``ValueError``) when the block is malformed.
    """
    path = cdk_json_path or Path.cwd() / "cdk.json"
    if not path.exists():
        raise RuntimeError(f"cdk.json not found at {path}")
    with open(path, encoding="utf-8") as handle:
        document = json.load(handle)
    context = document.get("context") if isinstance(document, dict) else None
    if not isinstance(context, dict):
        context = {}
    deployment_regions = context.get("deployment_regions")
    regional = deployment_regions.get("regional") if isinstance(deployment_regions, dict) else None
    raw = merge_eks_capabilities_overrides(
        context.get(EKS_CAPABILITIES_CONTEXT_KEY),
        parse_eks_capabilities_overrides(overrides),
    )
    return validate_eks_capabilities_config(raw, regional if isinstance(regional, list) else None)


def describe_live_capabilities(eks_client: Any, cluster_name: str) -> list[dict[str, Any]]:
    """Every capability attached to ``cluster_name``, fully described.

    ``ListCapabilities`` returns summaries (name, type, status, version);
    ``DescribeCapability`` adds the role ARN, health issues and the Argo CD
    server URL, so each summary is described in turn. Raises whatever boto3
    raises (the caller decides how a missing cluster reads).
    """
    summaries: list[dict[str, Any]] = []
    paginator = eks_client.get_paginator("list_capabilities")
    for page in paginator.paginate(clusterName=cluster_name):
        summaries.extend(item for item in page.get("capabilities") or [] if isinstance(item, dict))
    described: list[dict[str, Any]] = []
    for summary in summaries:
        name = summary.get("capabilityName")
        if not isinstance(name, str) or not name:
            continue
        response = eks_client.describe_capability(clusterName=cluster_name, capabilityName=name)
        detail = response.get("capability") if isinstance(response, dict) else None
        described.append(detail if isinstance(detail, dict) else dict(summary))
    return described


def _iso(value: Any) -> str | None:
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value) if value else None


def _health_issues(detail: Mapping[str, Any]) -> list[str]:
    health = detail.get("health")
    issues = health.get("issues") if isinstance(health, Mapping) else None
    rendered: list[str] = []
    for issue in issues or []:
        if not isinstance(issue, Mapping):
            continue
        code = str(issue.get("code") or "Issue")
        message = str(issue.get("message") or "").strip()
        rendered.append(f"{code}: {message}" if message else code)
    return rendered


def _argocd_server_url(detail: Mapping[str, Any]) -> str | None:
    configuration = detail.get("configuration")
    argo = configuration.get("argoCd") if isinstance(configuration, Mapping) else None
    url = argo.get("serverUrl") if isinstance(argo, Mapping) else None
    return str(url) if url else None


def _gitops_summary(config: Mapping[str, Any], *, region: str, cluster_name: str) -> dict[str, Any]:
    """The rendered GitOps hand-off for this cluster, or ``{"enabled": False}``."""
    if not gitops_enabled_in_region(config, region):
        return {"enabled": False}
    gitops = config["argocd"]["gitops"]
    namespaces = [str(namespace) for namespace in gitops["destination_namespaces"]]
    source = gitops_source(config)
    summary: dict[str, Any] = {
        "enabled": True,
        "source": source,
        "repo_url": gitops_repository_url(
            config,
            region=region,
            cluster_name=cluster_name,
            url_suffix=aws_url_suffix_for_region(region),
        ),
        "revision": str(gitops["revision"]).strip(),
        "path": render_gitops_path(
            effective_gitops_path(gitops), region=region, cluster_name=cluster_name
        ),
        "destination_namespaces": namespaces,
        "sync_policy": str(gitops["sync_policy"]),
        "project": GITOPS_PROJECT_NAME,
        "application": GITOPS_ROOT_APPLICATION_NAME,
    }
    if source == "codecommit":
        # The GCO-managed repository: what `gitops push` targets.
        summary["codecommit_repository"] = gitops_codecommit_repository_name(cluster_name)
        summary["codecommit_branch"] = GITOPS_CODECOMMIT_DEFAULT_BRANCH
    return summary


def _drift(*, configured: bool, detail: Mapping[str, Any] | None, stack_name: str) -> str | None:
    """One sentence naming how cdk.json and the cluster disagree, or ``None``."""
    if configured and detail is None:
        return f"configured in cdk.json but not attached; run 'gco stacks deploy {stack_name} -y'"
    if detail is not None and not configured:
        return (
            "attached but disabled in cdk.json; the next 'gco stacks deploy "
            f"{stack_name}' removes it (RETAIN keeps what it installed)"
        )
    if detail is not None:
        status = str(detail.get("status") or "UNKNOWN")
        if status != ACTIVE_STATUS:
            issues = _health_issues(detail)
            suffix = f" ({'; '.join(issues)})" if issues else ""
            return f"status is {status}, expected {ACTIVE_STATUS}{suffix}"
    return None


def build_status(
    *,
    region: str,
    project_name: str,
    config: Mapping[str, Any],
    live: Iterable[Mapping[str, Any]],
    cluster_found: bool = True,
) -> dict[str, Any]:
    """Merge the cdk.json intent with the described capabilities for one region.

    One row per capability type GCO knows (in canonical order), each stating
    whether cdk.json enables it for this region, whether it is attached, its
    live status/version/ARNs, health issues, and a ``drift`` sentence when the
    two disagree. Capabilities on the cluster that do not carry GCO's
    ``<project>-<type>`` name are listed under ``unmanaged`` so an operator's
    hand-made capability is visible but never mistaken for GCO's. ``healthy``
    is true only when nothing drifts.
    """
    cluster = cluster_name_for(project_name, region)
    stack_name = cluster
    live_by_name: dict[str, Mapping[str, Any]] = {}
    for item in live:
        name = item.get("capabilityName")
        if isinstance(name, str):
            live_by_name[name] = item

    rows: list[dict[str, Any]] = []
    for type_name in EKS_CAPABILITY_TYPES:
        name = capability_name(project_name, type_name)
        configured = capability_enabled_in_region(config, type_name, region)
        detail = live_by_name.pop(name, None)
        row: dict[str, Any] = {
            "type": type_name,
            "capability_name": name,
            "configured": configured,
            "deployed": detail is not None,
            "status": str(detail["status"]) if detail and detail.get("status") else None,
            "version": str(detail["version"]) if detail and detail.get("version") else None,
            "arn": str(detail["arn"]) if detail and detail.get("arn") else None,
            "role_arn": str(detail["roleArn"]) if detail and detail.get("roleArn") else None,
            "health_issues": _health_issues(detail) if detail else [],
            "modified_at": _iso(detail.get("modifiedAt")) if detail else None,
            "drift": _drift(configured=configured, detail=detail, stack_name=stack_name),
        }
        if type_name == "argocd":
            row["argocd_server_url"] = _argocd_server_url(detail) if detail else None
            row["gitops"] = _gitops_summary(config, region=region, cluster_name=cluster)
        rows.append(row)

    unmanaged = [
        {
            "capability_name": name,
            "type": _API_TYPE_TO_CONFIG.get(str(item.get("type")), str(item.get("type"))),
            "status": str(item.get("status")) if item.get("status") else None,
            "arn": str(item.get("arn")) if item.get("arn") else None,
        }
        for name, item in sorted(live_by_name.items())
    ]

    return {
        "region": region,
        "cluster_name": cluster,
        "cluster_found": cluster_found,
        "capabilities": rows,
        "unmanaged": unmanaged,
        "healthy": cluster_found and all(row["drift"] is None for row in rows),
    }


def capabilities_status(
    region: str,
    project_name: str,
    *,
    config: Mapping[str, Any] | None = None,
    eks_client: Any | None = None,
) -> dict[str, Any]:
    """``build_status`` for one region against the real EKS API.

    A cluster that does not exist (never deployed, or destroyed) is reported
    with ``cluster_found: false`` and no live rows rather than as an error, so
    ``--all-regions`` keeps going and a configured-but-undeployed region reads
    as drift instead of a crash.
    """
    from botocore.exceptions import ClientError

    if config is None:
        config = load_eks_capabilities_config()
    cluster = cluster_name_for(project_name, region)
    if eks_client is None:
        import boto3

        eks_client = boto3.client("eks", region_name=region)
    try:
        live = describe_live_capabilities(eks_client, cluster)
        cluster_found = True
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") != "ResourceNotFoundException":
            raise
        live = []
        cluster_found = False
    return build_status(
        region=region,
        project_name=project_name,
        config=config,
        live=live,
        cluster_found=cluster_found,
    )
