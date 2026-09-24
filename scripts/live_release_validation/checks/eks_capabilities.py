"""Checks behind the ``eks-capabilities`` action.

The opt-in EKS Capabilities (AWS-managed Argo CD, ACK and kro; see
``docs/EKS_CAPABILITIES.md``) are off in the shipped ``cdk.json``, and the
harness's preflight requires a clean worktree, so a run enables them the way
it enables optional schedulers: through run-scoped CDK context. The operator
passes the Identity Center inputs Argo CD needs on the command line, this
module turns them into the ``eks_capabilities_overrides`` JSON the deploy
carries, and the action later resolves the same merged block to know what to
prove.

What the action proves, per deployed Region:

* every enabled capability is attached to the Region's cluster and ``ACTIVE``
  — read through the same configured-vs-live merge ``gco stacks capabilities
  status`` uses (``cli.eks_capabilities``), so the CLI's drift model and the
  harness agree;
* for Argo CD, EKS published a server URL (the hosted UI the operator signs in
  to) and, through the tunnelled kubectl session, the objects
  ``07-argocd-cluster-access.yaml`` applied exist with the right identities:
  the ``local-cluster`` Secret naming the cluster by ARN and the RBAC bound to
  the capability role's access-entry group;
* for the GitOps hand-off, the fenced ``AppProject`` and the root
  ``Application`` exist and the Application reaches ``Synced`` / ``Healthy``
  within a bounded wait — with an automated sync policy that means the hosted
  Argo CD really pulled the repository path and wrote into the tenant
  namespace, which the fixture object under ``examples/gitops/tenant-smoke``
  then proves by carrying Argo CD's tracking label.
"""

from __future__ import annotations

import base64
import json
import re
import subprocess
import time
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from cli.eks_capabilities import build_status, describe_live_capabilities
from gco.eks_capabilities_config import (
    ARGOCD_LOCAL_CLUSTER_SECRET_NAME,
    ARGOCD_NAMESPACE,
    ARGOCD_SSO_IDENTITY_TYPES,
    EKS_CAPABILITY_TYPES,
    GITOPS_PROJECT_NAME,
    GITOPS_ROOT_APPLICATION_NAME,
    EksCapabilitiesConfigError,
    enabled_capability_types,
    gitops_enabled_in_region,
    merge_eks_capabilities_overrides,
    parse_eks_capabilities_overrides,
    validate_eks_capabilities_config,
)

from ..models import RunContext
from .cluster import KubectlRunner, kubectl_json

#: The repository path the run's GitOps hand-off points Argo CD at: a plain
#: directory of tenant manifests that must sync cleanly into ``gco-jobs``.
GITOPS_FIXTURE_PATH = "examples/gitops/tenant-smoke"
#: The one object the fixture path deploys; its presence with Argo CD's
#: tracking label is the proof the hosted Argo CD wrote into the tenant namespace.
GITOPS_FIXTURE_CONFIGMAP = "gco-gitops-tenant-smoke"
GITOPS_FIXTURE_NAMESPACE = "gco-jobs"
#: Argo CD's default resource-tracking label, set on every object it applies.
ARGOCD_TRACKING_LABEL = "app.kubernetes.io/instance"
#: The Kubernetes group EKS maps the capability role's access entry to.
ACCESS_ENTRY_GROUP_PREFIX = "eks-access-entry:"
#: How long the root Application may take to reach Synced/Healthy. The hosted
#: control plane polls the repository on its own cadence (minutes), then the
#: sync itself is a single ConfigMap.
GITOPS_SYNC_TIMEOUT_SECONDS = 900

_IDENTITY_RE = re.compile(r"^(SSO_USER|SSO_GROUP):(\S+)$")
_SCP_GIT_URL_RE = re.compile(r"^git@([^:]+):(.+)$")


class EksCapabilitiesValidationError(RuntimeError):
    """The deployed capabilities do not match what the run configured."""


# ─── run inputs -> eks_capabilities_overrides ────────────────────────────────


def parse_argocd_identity(value: str) -> dict[str, str]:
    """Parse ``SSO_USER:<id>`` / ``SSO_GROUP:<id>`` into an RBAC identity."""
    match = _IDENTITY_RE.match(value.strip())
    if match is None:
        raise ValueError(
            "expected TYPE:ID with TYPE one of "
            + ", ".join(ARGOCD_SSO_IDENTITY_TYPES)
            + f" (an Identity Center user or group id), got {value!r}"
        )
    return {"id": match.group(2), "type": match.group(1)}


def https_repository_url(remote_url: str) -> str:
    """Normalize a Git remote to the HTTPS form the hosted Argo CD can clone anonymously.

    ``git@github.com:owner/repo.git`` and ``ssh://git@github.com/owner/repo.git``
    become ``https://github.com/owner/repo.git``; HTTPS URLs pass through.
    """
    url = remote_url.strip()
    scp = _SCP_GIT_URL_RE.match(url)
    if scp:
        return f"https://{scp.group(1)}/{scp.group(2)}"
    if url.startswith("ssh://"):
        rest = url[len("ssh://") :]
        if "@" in rest.split("/", 1)[0]:
            rest = rest.split("@", 1)[1]
        return f"https://{rest}"
    return url


def default_gitops_repository_url(repo_root: Path) -> str | None:
    """The checkout's ``origin`` remote as an HTTPS URL, or ``None`` when unknown."""
    result = subprocess.run(
        ["git", "-C", str(repo_root), "config", "--get", "remote.origin.url"],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0 or not result.stdout.strip():
        return None
    return https_repository_url(result.stdout.strip())


def build_eks_capabilities_overrides(
    *,
    types: tuple[str, ...],
    idc_instance_arn: str | None,
    idc_region: str | None,
    identities: tuple[str, ...],
    gitops: bool,
    repo_url: str | None,
    revision: str | None,
    path: str,
    sync_policy: str,
) -> dict[str, Any]:
    """Turn the run's command-line inputs into the ``eks_capabilities_overrides`` object.

    Every named type is enabled for every Region the run deploys. Argo CD
    additionally needs the Identity Center instance and at least one identity
    (mapped to ``ADMIN`` so the operator can sign in and inspect the UI); the
    GitOps hand-off points the Application at ``repo_url``/``revision``/``path``
    with ``gco-jobs`` as its only destination — the fixture is a single tenant
    ConfigMap. Raises ``ValueError`` on an incomplete Argo CD request.
    """
    unknown = sorted(set(types) - set(EKS_CAPABILITY_TYPES))
    if unknown:
        raise ValueError(
            "unknown EKS capability type(s): "
            + ", ".join(unknown)
            + "; valid: "
            + ", ".join(EKS_CAPABILITY_TYPES)
        )
    overrides: dict[str, Any] = {
        name: {"enabled": True} for name in EKS_CAPABILITY_TYPES if name in types
    }
    if "argocd" in types:
        if not idc_instance_arn or not idc_instance_arn.startswith("arn:"):
            raise ValueError(
                "--argocd-idc-instance-arn (the IAM Identity Center instance ARN) is required "
                "when the argocd capability is requested"
            )
        if not identities:
            raise ValueError(
                "at least one --argocd-identity TYPE:ID is required when the argocd capability "
                "is requested (the hosted Argo CD has no local users)"
            )
        argocd: dict[str, Any] = {
            "enabled": True,
            "idc_instance_arn": idc_instance_arn,
            "rbac_role_mappings": [
                {
                    "role": "ADMIN",
                    "identities": [parse_argocd_identity(item) for item in identities],
                }
            ],
        }
        if idc_region:
            argocd["idc_region"] = idc_region
        if gitops:
            if not repo_url:
                raise ValueError(
                    "--argocd-gitops-repo-url is required for the GitOps hand-off (no origin "
                    "remote could be derived)"
                )
            if not revision:
                raise ValueError("--argocd-gitops-revision is required for the GitOps hand-off")
            argocd["gitops"] = {
                "enabled": True,
                "repo_url": repo_url,
                "revision": revision,
                "path": path,
                "destination_namespaces": [GITOPS_FIXTURE_NAMESPACE],
                "sync_policy": sync_policy,
            }
        overrides["argocd"] = argocd
    return overrides


def overrides_json(overrides: Mapping[str, Any]) -> str:
    """Canonical JSON for the ``--context`` value and the resume identity."""
    return json.dumps(overrides, sort_keys=True, separators=(",", ":"))


# ─── effective configuration ─────────────────────────────────────────────────


def effective_eks_capabilities_config(ctx: RunContext) -> dict[str, Any]:
    """The block the run deployed with: cdk.json merged with the run's overrides."""
    overrides_raw = getattr(ctx.settings, "eks_capabilities_overrides_json", "")
    try:
        raw = merge_eks_capabilities_overrides(
            ctx.cdk_context.get("eks_capabilities"),
            parse_eks_capabilities_overrides(overrides_raw or None),
        )
        return validate_eks_capabilities_config(raw, ctx.deployment_regions)
    except EksCapabilitiesConfigError as exc:
        raise EksCapabilitiesValidationError(f"eks_capabilities is invalid: {exc}") from exc


def enabled_types_by_region(
    config: Mapping[str, Any], regions: tuple[str, ...]
) -> dict[str, list[str]]:
    """``{region: [types]}`` for the Regions where at least one type is on."""
    result: dict[str, list[str]] = {}
    for region in regions:
        enabled = enabled_capability_types(config, region)
        if enabled:
            result[region] = enabled
    return result


# ─── AWS-side verification ───────────────────────────────────────────────────


def verify_capabilities_attached(
    ctx: RunContext,
    region: str,
    config: Mapping[str, Any],
) -> dict[str, Any]:
    """Require every enabled type ACTIVE on the Region's cluster; return the CLI status document.

    Uses ``cli.eks_capabilities.build_status`` so a failure here reads exactly
    like ``gco stacks capabilities status`` would report it.
    """
    eks = ctx.session.client("eks", region_name=region)
    project_name = ctx.config.project_name
    cluster_name = f"{project_name}-{region}"
    cluster = eks.describe_cluster(name=cluster_name).get("cluster") or {}
    cluster_arn = str(cluster.get("arn") or "")
    if not cluster_arn:
        raise EksCapabilitiesValidationError(f"{region}: EKS returned no ARN for {cluster_name}")
    live = describe_live_capabilities(eks, cluster_name)
    status = build_status(region=region, project_name=project_name, config=config, live=live)
    problems = [f"{row['type']}: {row['drift']}" for row in status["capabilities"] if row["drift"]]
    if problems:
        raise EksCapabilitiesValidationError(
            f"{region}: capabilities drift from the run configuration: " + "; ".join(problems)
        )
    argo = status["capabilities"][0]
    if argo["configured"] and not argo["argocd_server_url"]:
        raise EksCapabilitiesValidationError(
            f"{region}: the Argo CD capability is ACTIVE but EKS published no server URL"
        )
    status["cluster_arn"] = cluster_arn
    return status


# ─── cluster-side verification (Argo CD) ─────────────────────────────────────


def _dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def verify_argocd_cluster_access(
    kubectl: KubectlRunner,
    record: dict[str, Any],
    *,
    cluster_arn: str,
    role_arn: str,
    timeout: float,
) -> dict[str, Any]:
    """The objects ``07-argocd-cluster-access.yaml`` applied, with the right identities."""
    secret = kubectl_json(
        kubectl,
        record,
        "get",
        "secret",
        ARGOCD_LOCAL_CLUSTER_SECRET_NAME,
        "--namespace",
        ARGOCD_NAMESPACE,
        timeout=timeout,
    )
    if secret is None:
        raise EksCapabilitiesValidationError(
            f"{ARGOCD_NAMESPACE}/{ARGOCD_LOCAL_CLUSTER_SECRET_NAME} Secret is absent: the hosted "
            "Argo CD has no registered deployment target"
        )
    encoded = _dict(secret.get("data")).get("server")
    server = base64.b64decode(encoded).decode("utf-8") if isinstance(encoded, str) else ""
    if server != cluster_arn:
        raise EksCapabilitiesValidationError(
            f"local-cluster Secret registers {server!r}, expected the cluster ARN {cluster_arn!r}"
        )
    labels = _dict(_dict(secret.get("metadata")).get("labels"))
    if labels.get("argocd.argoproj.io/secret-type") != "cluster":
        raise EksCapabilitiesValidationError(
            "local-cluster Secret lacks the argocd.argoproj.io/secret-type=cluster label"
        )

    group = f"{ACCESS_ENTRY_GROUP_PREFIX}{role_arn}"
    binding = kubectl_json(
        kubectl, record, "get", "clusterrolebinding", "gco-argocd-read-all", timeout=timeout
    )
    if binding is None:
        raise EksCapabilitiesValidationError("ClusterRoleBinding gco-argocd-read-all is absent")
    subjects = [
        (str(item.get("kind")), str(item.get("name")))
        for item in binding.get("subjects") or []
        if isinstance(item, dict)
    ]
    if ("Group", group) not in subjects:
        raise EksCapabilitiesValidationError(
            f"gco-argocd-read-all binds {subjects}, not the capability role's access-entry "
            f"group {group!r}"
        )
    for namespace in ("gco-jobs", "gco-inference"):
        role_binding = kubectl_json(
            kubectl,
            record,
            "get",
            "rolebinding",
            "gco-argocd-deploy",
            "--namespace",
            namespace,
            timeout=timeout,
        )
        if role_binding is None:
            raise EksCapabilitiesValidationError(
                f"{namespace}/gco-argocd-deploy RoleBinding is absent"
            )
    return {"server": server, "access_entry_group": group}


def _application_state(application: Mapping[str, Any]) -> dict[str, Any]:
    status = _dict(application.get("status"))
    sync = _dict(status.get("sync"))
    health = _dict(status.get("health"))
    operation = _dict(status.get("operationState"))
    return {
        "sync_status": sync.get("status"),
        "revision": sync.get("revision"),
        "health_status": health.get("status"),
        "health_message": health.get("message"),
        "operation_phase": operation.get("phase"),
        "operation_message": operation.get("message"),
        "conditions": [
            {"type": item.get("type"), "message": item.get("message")}
            for item in status.get("conditions") or []
            if isinstance(item, dict)
        ],
    }


def wait_for_gitops_sync(
    kubectl: KubectlRunner,
    record: dict[str, Any],
    *,
    expected_revision: str,
    expected_repo_url: str,
    poll_interval: float,
    timeout: float,
    deadline_seconds: float = GITOPS_SYNC_TIMEOUT_SECONDS,
) -> dict[str, Any]:
    """Poll the root Application until it is Synced and Healthy at the expected revision.

    Argo CD's own error conditions (``ComparisonError``, ``SyncError``,
    ``InvalidSpecError``) are terminal for this run — waiting will not fix a
    wrong repository, path or fence — so they fail immediately with the
    Application's status as evidence. Everything else is polled to the
    deadline.
    """
    project = kubectl_json(
        kubectl,
        record,
        "get",
        "appproject",
        GITOPS_PROJECT_NAME,
        "--namespace",
        ARGOCD_NAMESPACE,
        timeout=timeout,
    )
    if project is None:
        raise EksCapabilitiesValidationError(f"AppProject {GITOPS_PROJECT_NAME} is absent")
    sources = _dict(project.get("spec")).get("sourceRepos") or []
    if expected_repo_url not in sources:
        raise EksCapabilitiesValidationError(
            f"AppProject {GITOPS_PROJECT_NAME} allows {sources}, not the run's repository "
            f"{expected_repo_url!r}"
        )

    # A run pins the exact commit under validation, so the synced revision
    # must be that SHA; a branch or tag name resolves to whatever it points
    # at, so only Synced/Healthy can be required then.
    revision_pinned = re.fullmatch(r"[0-9a-fA-F]{40}", expected_revision) is not None
    deadline = time.monotonic() + deadline_seconds
    samples: list[dict[str, Any]] = []
    while True:
        application = kubectl_json(
            kubectl,
            record,
            "get",
            "application",
            GITOPS_ROOT_APPLICATION_NAME,
            "--namespace",
            ARGOCD_NAMESPACE,
            timeout=timeout,
        )
        if application is None:
            raise EksCapabilitiesValidationError(
                f"Application {GITOPS_ROOT_APPLICATION_NAME} is absent"
            )
        state = _application_state(application)
        samples.append(state)
        record["gitops_samples"] = samples[-10:]
        terminal = [
            item for item in state["conditions"] if str(item.get("type") or "").endswith("Error")
        ]
        if terminal:
            raise EksCapabilitiesValidationError(
                f"Application {GITOPS_ROOT_APPLICATION_NAME} reports error conditions: "
                + "; ".join(f"{item['type']}: {item['message']}" for item in terminal)
            )
        revision_ok = (
            str(state["revision"] or "").lower() == expected_revision.lower()
            if revision_pinned
            else True
        )
        if state["sync_status"] == "Synced" and state["health_status"] == "Healthy" and revision_ok:
            return state
        if time.monotonic() >= deadline:
            raise EksCapabilitiesValidationError(
                f"Application {GITOPS_ROOT_APPLICATION_NAME} did not reach Synced/Healthy at "
                f"{expected_revision} within {int(deadline_seconds)}s; last state: {state}"
            )
        time.sleep(poll_interval)


def verify_gitops_fixture(
    kubectl: KubectlRunner,
    record: dict[str, Any],
    *,
    timeout: float,
) -> dict[str, Any]:
    """The fixture ConfigMap exists in the tenant namespace and carries Argo CD's tracking label."""
    configmap = kubectl_json(
        kubectl,
        record,
        "get",
        "configmap",
        GITOPS_FIXTURE_CONFIGMAP,
        "--namespace",
        GITOPS_FIXTURE_NAMESPACE,
        timeout=timeout,
    )
    if configmap is None:
        raise EksCapabilitiesValidationError(
            f"{GITOPS_FIXTURE_NAMESPACE}/{GITOPS_FIXTURE_CONFIGMAP} is absent although the "
            "Application reports Synced"
        )
    labels = _dict(_dict(configmap.get("metadata")).get("labels"))
    if labels.get(ARGOCD_TRACKING_LABEL) != GITOPS_ROOT_APPLICATION_NAME:
        raise EksCapabilitiesValidationError(
            f"{GITOPS_FIXTURE_CONFIGMAP} lacks Argo CD's tracking label "
            f"{ARGOCD_TRACKING_LABEL}={GITOPS_ROOT_APPLICATION_NAME}: {labels}"
        )
    return {"labels": labels, "data": _dict(configmap.get("data"))}


def gitops_expectations(config: Mapping[str, Any], region: str) -> dict[str, str] | None:
    """The repository URL and revision the Region's hand-off must have synced, or None."""
    if not gitops_enabled_in_region(config, region):
        return None
    gitops = config["argocd"]["gitops"]
    return {
        "repo_url": str(gitops["repo_url"]).strip(),
        "revision": str(gitops["revision"]).strip(),
        "path": str(gitops["path"]).strip(),
    }
