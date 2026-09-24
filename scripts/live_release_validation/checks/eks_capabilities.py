"""Checks behind the ``eks-capabilities`` action.

The opt-in EKS Capabilities (AWS-managed Argo CD, ACK and kro; see
``docs/EKS_CAPABILITIES.md``) are off in the shipped ``cdk.json``, and the
harness's preflight requires a clean worktree, so a run enables them the way
it enables optional schedulers: through run-scoped CDK context. The run is
self-contained: nothing has to exist in the account beforehand. The
``argocd-identity`` action (before ``deploy``) discovers or creates the IAM
Identity Center account instance and the admin group the hosted Argo CD
authenticates against, the deploy creates the per-cluster CodeCommit
repository the hand-off reads (``gitops.source: codecommit``), and this
action pushes the fixture directory into it before proving the sync. Operator
inputs (``--argocd-idc-instance-arn``, ``--argocd-identity``,
``--argocd-gitops-repo-url``) replace any of those steps with pre-existing
resources. This module turns the inputs into the ``eks_capabilities_overrides``
JSON the deploy carries (merging in whatever the identity action provisioned)
and later resolves the same merged block to know what to prove.

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

from cli.eks_capabilities import build_status, cluster_name_for, describe_live_capabilities
from cli.gitops_push import PushResult, push_gitops_repository
from gco.eks_capabilities_config import (
    ARGOCD_LOCAL_CLUSTER_SECRET_NAME,
    ARGOCD_NAMESPACE,
    ARGOCD_SSO_IDENTITY_TYPES,
    EKS_CAPABILITY_TYPES,
    GITOPS_PROJECT_NAME,
    GITOPS_ROOT_APPLICATION_NAME,
    EksCapabilitiesConfigError,
    aws_url_suffix_for_region,
    effective_gitops_path,
    enabled_capability_types,
    gitops_codecommit_repository_name,
    gitops_enabled_in_region,
    gitops_repository_url,
    gitops_source,
    merge_eks_capabilities_overrides,
    parse_eks_capabilities_overrides,
    render_gitops_path,
    validate_eks_capabilities_config,
)

from ..models import RunCheckpoint, RunContext, RunSettings
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

    Every named type is enabled for every Region the run deploys. For Argo CD
    the Identity Center inputs are optional: whatever the operator passed
    (``--argocd-idc-instance-arn``, ``--argocd-identity``) is carried as-is,
    and what is missing is provisioned by the ``argocd-identity`` action
    before deploy (see :func:`needs_identity_bootstrap`) and merged in by
    :func:`effective_overrides`. The GitOps hand-off defaults to the
    GCO-managed CodeCommit repository (``source: codecommit``; the action
    pushes the fixture directory into it after deploy); a ``repo_url`` selects
    ``source: git`` with ``revision``/``path`` inside that repository. Either
    way ``gco-jobs`` is the only destination — the fixture is one tenant
    ConfigMap. Raises ``ValueError`` on an inconsistent request.
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
        argocd: dict[str, Any] = {"enabled": True}
        if idc_instance_arn:
            if not idc_instance_arn.startswith("arn:"):
                raise ValueError(
                    f"--argocd-idc-instance-arn must be an Identity Center instance ARN, got "
                    f"{idc_instance_arn!r}"
                )
            argocd["idc_instance_arn"] = idc_instance_arn
        if idc_region:
            argocd["idc_region"] = idc_region
        if identities:
            argocd["rbac_role_mappings"] = [
                {
                    "role": "ADMIN",
                    "identities": [parse_argocd_identity(item) for item in identities],
                }
            ]
        if gitops:
            gitops_block: dict[str, Any] = {
                "enabled": True,
                "destination_namespaces": [GITOPS_FIXTURE_NAMESPACE],
                "sync_policy": sync_policy,
            }
            if repo_url:
                if not revision:
                    raise ValueError(
                        "--argocd-gitops-revision is required with --argocd-gitops-repo-url"
                    )
                gitops_block.update(
                    {"source": "git", "repo_url": repo_url, "revision": revision, "path": path}
                )
            else:
                gitops_block["source"] = "codecommit"
            argocd["gitops"] = gitops_block
        overrides["argocd"] = argocd
    return overrides


def needs_identity_bootstrap(overrides: Mapping[str, Any]) -> bool:
    """True when Argo CD is requested without a complete Identity Center configuration."""
    argocd = overrides.get("argocd")
    if not isinstance(argocd, Mapping) or argocd.get("enabled") is not True:
        return False
    return not argocd.get("idc_instance_arn") or not argocd.get("rbac_role_mappings")


def overrides_json(overrides: Mapping[str, Any]) -> str:
    """Canonical JSON for the ``--context`` value and the resume identity."""
    return json.dumps(overrides, sort_keys=True, separators=(",", ":"))


# ─── the provisioned Identity Center inputs ──────────────────────────────────

#: Checkpoint state key holding what the ``argocd-identity`` action resolved.
IDENTITY_STATE_KEY = "argocd_identity"
#: Names of the Identity Center resources the harness owns. Deterministic so a
#: crashed run's leftovers are recognized (and deleted) by the next run and
#: flagged by the inventory scanners; an operator's own instance or group never
#: carries them.
IDENTITY_INSTANCE_NAME_SUFFIX = "live-validation"
IDENTITY_GROUP_NAME_SUFFIX = "live-validation-argocd"


def identity_instance_name(project_name: str) -> str:
    return f"{project_name}-{IDENTITY_INSTANCE_NAME_SUFFIX}"


def identity_group_name(project_name: str) -> str:
    return f"{project_name}-{IDENTITY_GROUP_NAME_SUFFIX}"


def identity_state(checkpoint: RunCheckpoint) -> dict[str, Any] | None:
    """The ``argocd-identity`` action's record, or ``None`` before it ran."""
    state = checkpoint.state.get(IDENTITY_STATE_KEY)
    return dict(state) if isinstance(state, Mapping) else None


def effective_overrides(
    static_overrides_json: str,
    identity: Mapping[str, Any] | None,
    *,
    require_identity: bool = False,
) -> dict[str, Any]:
    """The run's overrides with the provisioned Identity Center inputs merged in.

    The static part (types, GitOps shape, any operator-supplied Identity
    Center inputs) is fixed at settings time and is part of the resume
    identity; the provisioned part lives in the checkpoint and is layered on
    here, so every CDK invocation and every check sees one block.

    Until the ``argocd-identity`` action has recorded that part there is no
    Identity Center instance to bind the capability to, and the CDK app
    rightly refuses an Argo CD block without one. A block that still needs
    the bootstrap therefore synthesizes *without* Argo CD: the only CDK
    invocation that precedes the action is preflight's ``cdk list``, which
    enumerates the same stacks either way (capabilities are resources inside
    the regional stack), and ``deploy`` depends on ``argocd-identity``
    re-registering the merged context. Callers that read the block to learn
    what was deployed pass ``require_identity=True`` and fail closed instead
    of silently seeing a run without Argo CD.
    """
    overrides = parse_eks_capabilities_overrides(static_overrides_json or None)
    if not overrides:
        return overrides
    argocd = overrides.get("argocd")
    if not isinstance(argocd, dict) or argocd.get("enabled") is not True:
        return overrides
    if not identity:
        if needs_identity_bootstrap(overrides):
            if require_identity:
                raise EksCapabilitiesValidationError(
                    "Argo CD is enabled for this run but the argocd-identity action has not "
                    "recorded its Identity Center inputs in the checkpoint"
                )
            del overrides["argocd"]
        return overrides
    argocd.setdefault("idc_instance_arn", str(identity["instance_arn"]))
    if identity.get("idc_region"):
        argocd.setdefault("idc_region", str(identity["idc_region"]))
    if not argocd.get("rbac_role_mappings") and identity.get("group_id"):
        argocd["rbac_role_mappings"] = [
            {
                "role": "ADMIN",
                "identities": [{"id": str(identity["group_id"]), "type": "SSO_GROUP"}],
            }
        ]
    return overrides


def effective_cdk_context(settings: RunSettings, checkpoint: RunCheckpoint) -> dict[str, str]:
    """``settings.extra_cdk_context()`` with the provisioned identity merged into the overrides.

    Before ``argocd-identity`` has run, a block that still needs the bootstrap
    carries no Argo CD (see :func:`effective_overrides`); when nothing is left
    to override the key is dropped rather than passed as an empty object.
    """
    context = dict(settings.extra_cdk_context())
    static = getattr(settings, "eks_capabilities_overrides_json", "")
    if static:
        merged = effective_overrides(static, identity_state(checkpoint))
        if merged:
            context["eks_capabilities_overrides"] = overrides_json(merged)
        else:
            context.pop("eks_capabilities_overrides", None)
    return context


def apply_effective_cdk_context(ctx: RunContext) -> dict[str, str]:
    """Register the effective context with the stack manager; returns what was applied."""
    context = effective_cdk_context(ctx.settings, ctx.checkpoint)
    if context:
        ctx.stack_manager.set_extra_cdk_context(context)
    return context


def _identity_client_factory(ctx: RunContext) -> Any:
    def factory(service_name: str, region: str) -> Any:
        return ctx.session.client(service_name, region_name=region)

    return factory


def bootstrap_validation_identity(ctx: RunContext) -> dict[str, Any]:
    """Resolve or create the Identity Center inputs for this run and record them.

    Reuses whatever instance is visible from the account (looking in the
    requested Identity Center Region first, then every Identity Center Region);
    creates an *account instance* named ``<project>-live-validation`` when there
    is none; ensures the group ``<project>-live-validation-argocd`` and maps it
    to ``ADMIN``. Operator-supplied identities skip the group entirely. The
    record is idempotent under ``--resume``: a second call returns the stored
    state without touching AWS.
    """
    from cli.argocd_identity import bootstrap_argocd_identity

    existing = identity_state(ctx.checkpoint)
    if existing is not None:
        return existing
    static = parse_eks_capabilities_overrides(
        getattr(ctx.settings, "eks_capabilities_overrides_json", "") or None
    )
    static_argocd = static.get("argocd")
    argocd: Mapping[str, Any] = static_argocd if isinstance(static_argocd, Mapping) else {}
    supplied_identities: tuple[dict[str, str], ...] = tuple(
        {"id": str(identity["id"]), "type": str(identity["type"])}
        for mapping in argocd.get("rbac_role_mappings") or []
        for identity in mapping.get("identities") or []
    )
    project_name = ctx.config.project_name
    preferred_region = (
        str(getattr(ctx.settings, "argocd_idc_region", "") or "")
        or str(argocd.get("idc_region") or "")
        or ctx.deployment_regions[0]
    )
    result = bootstrap_argocd_identity(
        account_id=ctx.settings.expected_account,
        project_name=project_name,
        preferred_region=preferred_region,
        client_factory=_identity_client_factory(ctx),
        instance_arn=str(argocd.get("idc_instance_arn") or "") or None,
        create_account_instance_if_missing=True,
        instance_name=identity_instance_name(project_name),
        instance_tags={"gco:project": project_name, "gco:live-validation-run": ctx.settings.run_id},
        group_name=identity_group_name(project_name),
        identities=supplied_identities,
    )
    record: dict[str, Any] = {
        "instance_arn": result.instance.instance_arn,
        "identity_store_id": result.instance.identity_store_id,
        "idc_region": result.instance.region,
        "instance_name": result.instance.name,
        "instance_owner_account_id": result.instance.owner_account_id,
        "instance_created": result.instance_created,
        # Harness-owned by name: deleted at cleanup even when a crashed run
        # left it behind and this run merely reused it.
        "instance_harness_owned": (
            result.instance.name == identity_instance_name(project_name)
            and result.instance.owned_by(ctx.settings.expected_account)
        ),
        "group_id": result.group_id,
        "group_name": result.group_name,
        "group_created": result.group_created,
        "run_tag": ctx.settings.run_id,
        "role_mapping": result.role_mapping(),
    }
    with ctx.state_lock:
        ctx.checkpoint.state[IDENTITY_STATE_KEY] = record
    ctx.persist()
    return record


def cleanup_validation_identity(ctx: RunContext) -> dict[str, Any]:
    """Delete the harness-owned Identity Center group and instance, if any.

    The group goes first (it lives in the instance's store), then the
    instance when this run created it or it carries the harness's name. An
    operator-provided or organization instance is never touched. Runs after
    the stacks are gone so the capability's managed application no longer
    references the instance.
    """
    from cli.argocd_identity import delete_account_instance, delete_group

    record = identity_state(ctx.checkpoint)
    result: dict[str, Any] = {"performed": False, "group_deleted": False, "instance_deleted": False}
    if record is None:
        return result
    result["performed"] = True
    factory = _identity_client_factory(ctx)
    region = str(record["idc_region"])
    group_id = record.get("group_id")
    if group_id and record.get("group_name") == identity_group_name(ctx.config.project_name):
        result["group_deleted"] = delete_group(
            factory("identitystore", region),
            identity_store_id=str(record["identity_store_id"]),
            group_id=str(group_id),
        )
    if record.get("instance_created") or record.get("instance_harness_owned"):
        result["instance_deleted"] = delete_account_instance(
            factory, region, str(record["instance_arn"])
        )
    with ctx.state_lock:
        ctx.checkpoint.state.setdefault("argocd_identity_cleanup", []).append(result)
    ctx.persist()
    return result


# ─── effective configuration ─────────────────────────────────────────────────


def effective_eks_capabilities_config(ctx: RunContext) -> dict[str, Any]:
    """The block the run deployed with: cdk.json merged with the run's effective overrides."""
    try:
        overrides = effective_overrides(
            getattr(ctx.settings, "eks_capabilities_overrides_json", ""),
            identity_state(ctx.checkpoint),
            require_identity=True,
        )
        raw = merge_eks_capabilities_overrides(ctx.cdk_context.get("eks_capabilities"), overrides)
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


def gitops_expectations(
    config: Mapping[str, Any], region: str, *, project_name: str
) -> dict[str, Any] | None:
    """What the Region's hand-off must have synced, or None when it is off.

    ``source: codecommit`` names the GCO-managed repository the action pushes
    the fixture into (its URL is what the ``AppProject`` must allow); ``source:
    git`` carries the operator repository, the pinned revision and the path.
    """
    if not gitops_enabled_in_region(config, region):
        return None
    gitops = config["argocd"]["gitops"]
    cluster_name = cluster_name_for(project_name, region)
    source = gitops_source(config)
    expectations: dict[str, Any] = {
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
    }
    if source == "codecommit":
        expectations["repository_name"] = gitops_codecommit_repository_name(cluster_name)
    return expectations


def push_gitops_fixture(
    ctx: RunContext,
    region: str,
    config: Mapping[str, Any],
    *,
    fixture_dir: Path,
) -> PushResult:
    """Mirror the fixture directory into the Region's GCO-managed repository.

    The same code path operators use (``gco stacks capabilities gitops push``),
    through the harness's throttle-resilient session. The returned commit id
    is what the root ``Application`` must report as its synced revision.
    """
    return push_gitops_repository(
        region,
        ctx.config.project_name,
        fixture_dir,
        config=config,
        message=f"gco live release validation {ctx.settings.run_id}: {fixture_dir.name} @ "
        f"{ctx.settings.expected_sha}",
        codecommit_client=ctx.session.client("codecommit", region_name=region),
    )
