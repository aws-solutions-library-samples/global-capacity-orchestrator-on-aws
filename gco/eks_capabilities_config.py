"""Schema, defaults and validation for the ``eks_capabilities`` cdk.json block.

EKS Capabilities are AWS-managed installations of Argo CD, AWS Controllers for
Kubernetes (ACK) and kro that run in an AWS-owned account and attach to a
cluster as ``AWS::EKS::Capability`` resources. GCO exposes them as opt-in
per-type knobs: every type is **off by default**, each enabled type
synthesizes one capability IAM role and one capability per selected regional
cluster, and Argo CD additionally supports a declarative GitOps hand-off (a
fenced ``AppProject`` plus one root ``Application``) that points each selected
cluster's Argo CD at a repository path: by default a GCO-managed per-cluster
AWS CodeCommit repository (``source: codecommit``), otherwise an
operator-owned repository (``source: git``).

This module owns the block's shape so the config loader, the regional stack,
the CLI and the live-validation harness all agree on names and defaults. Like
``gco.enablement_overrides`` and ``gco.inference_proxy_config`` it lives at
the package top level and imports nothing from ``aws_cdk``: the ``gco`` CLI
runs without the CDK toolchain installed, so it must be able to read the
block without pulling ``gco.config`` (whose package import loads the CDK
app). It deliberately raises :class:`EksCapabilitiesConfigError` (a
``ValueError``) rather than the loader's ``ConfigValidationError``; the
loader re-raises with its own exception type.
"""

from __future__ import annotations

import copy
import json
from collections.abc import Iterable, Mapping
from typing import Any

#: cdk.json context key.
EKS_CAPABILITIES_CONTEXT_KEY = "eks_capabilities"

#: Run-scoped CDK context key carrying a JSON object deep-merged over the
#: cdk.json block for one deploy (``cdk deploy --context
#: eks_capabilities_overrides='{"kro": {"enabled": true}}'``). The
#: capabilities sibling of ``helm_enabled_overrides`` / ``feature_enabled_overrides``:
#: the live-validation harness, whose preflight requires a clean worktree,
#: enables Argo CD with the operator's Identity Center inputs this way.
EKS_CAPABILITIES_OVERRIDES_CONTEXT_KEY = "eks_capabilities_overrides"

#: Capability types GCO can attach, keyed the way cdk.json spells them.
EKS_CAPABILITY_TYPES: tuple[str, ...] = ("argocd", "ack", "kro")

#: cdk.json spelling -> ``AWS::EKS::Capability`` ``Type`` value.
CAPABILITY_TYPE_API_NAMES: dict[str, str] = {"argocd": "ARGOCD", "ack": "ACK", "kro": "KRO"}

#: Kubernetes namespace the hosted Argo CD reads its ``Application`` /
#: ``AppProject`` / cluster-secret objects from. Fixed so the kubectl-applier
#: manifests, the prune inventory and the live checks agree.
ARGOCD_NAMESPACE = "argocd"

#: Argo CD cluster-secret name registering the hosting cluster itself as a
#: deployment target ("local cluster"; the hosted capability does not register
#: it automatically).
ARGOCD_LOCAL_CLUSTER_SECRET_NAME = "local-cluster"  # nosec B105  # Kubernetes Secret name, not its contents

#: Fixed object names of the GitOps hand-off so the applier's prune inventory
#: can name them exactly.
GITOPS_PROJECT_NAME = "gco-tenants"
GITOPS_ROOT_APPLICATION_NAME = "gco-gitops-root"

#: Namespaces the GitOps hand-off may deploy into. These are the two tenant
#: namespaces GCO ships RBAC for; the platform namespace ``gco-system`` is
#: deliberately never a destination so a compromised tenant repository cannot
#: touch platform resources.
GITOPS_TENANT_NAMESPACES: tuple[str, ...] = ("gco-jobs", "gco-inference")

ARGOCD_RBAC_ROLES: tuple[str, ...] = ("ADMIN", "EDITOR", "VIEWER")
ARGOCD_SSO_IDENTITY_TYPES: tuple[str, ...] = ("SSO_USER", "SSO_GROUP")
GITOPS_SYNC_POLICIES: tuple[str, ...] = ("manual", "automated")

#: Where the hand-off's manifests come from. ``codecommit`` (the default) is
#: the batteries-included source: the regional stack creates one AWS
#: CodeCommit repository per selected cluster, grants the capability role
#: ``codecommit:GitPull`` on exactly that repository, and operators fill it
#: with ``gco stacks capabilities gitops push`` — no credentials, no
#: repository Secret, nothing to reach over the public internet (the hosted
#: Argo CD authenticates to CodeCommit with its own IAM role). ``git`` points
#: at an operator-owned repository ``repo_url`` instead (public, or private
#: through Secrets Manager credentials / CodeConnections).
GITOPS_SOURCES: tuple[str, ...] = ("codecommit", "git")

#: ``path`` used when the block leaves it empty, per source: a per-cluster
#: CodeCommit repository is read from its root, a shared operator repository
#: from a per-cluster overlay directory.
GITOPS_DEFAULT_PATHS: dict[str, str] = {"codecommit": ".", "git": "clusters/{region}"}

#: Branch the seeded CodeCommit repository starts with (also what
#: ``gitops push`` targets by default); ``revision: HEAD`` follows it.
GITOPS_CODECOMMIT_DEFAULT_BRANCH = "main"

#: What ``cdk destroy`` does with a GCO-managed CodeCommit repository. The
#: operator's local checkout is the source of truth (the repository only ever
#: receives what ``gitops push`` mirrors into it), so the default follows the
#: project convention of leaving nothing behind; ``retain`` keeps the history.
GITOPS_CODECOMMIT_REMOVAL_POLICIES: tuple[str, ...] = ("destroy", "retain")

#: Placeholders the GitOps ``path`` may carry; substituted per cluster so one
#: repository can hold a per-cluster overlay directory.
GITOPS_PATH_PLACEHOLDERS: tuple[str, ...] = ("{region}", "{cluster_name}")

#: The full block with every knob at its default. ``regions: []`` means every
#: regional deployment region; a non-empty list restricts the type to that
#: subset of clusters.
EKS_CAPABILITIES_DEFAULTS: dict[str, Any] = {
    "argocd": {
        "enabled": False,
        "regions": [],
        # IAM Identity Center instance ARN (required: hosted Argo CD has no
        # local users) and, when the instance lives elsewhere, its region.
        "idc_instance_arn": "",
        "idc_region": "",
        # [{"role": ADMIN|EDITOR|VIEWER, "identities": [{"id", "type": SSO_USER|SSO_GROUP}]}]
        "rbac_role_mappings": [],
        # Interface VPC endpoint ids (com.amazonaws.<region>.eks-capabilities)
        # that make the Argo CD UI/API private. Empty keeps the public endpoint.
        "vpce_ids": [],
        # Secrets Manager secret ARNs holding Git repository credentials; the
        # capability role is granted read on exactly these.
        "repo_credentials_secret_arns": [],
        # Customer-managed KMS key ARNs those secrets are encrypted with; the
        # role gets kms:Decrypt on exactly these, via Secrets Manager only.
        # Secrets under the AWS-managed aws/secretsmanager key need nothing here.
        "repo_credentials_kms_key_arns": [],
        "gitops": {
            "enabled": False,
            # codecommit: one GCO-managed CodeCommit repository per selected
            # cluster (push with `gco stacks capabilities gitops push`);
            # git: the operator-owned repository in repo_url.
            "source": "codecommit",
            "repo_url": "",
            "revision": "HEAD",
            # "" means the source's default (GITOPS_DEFAULT_PATHS).
            "path": "",
            "destination_namespaces": list(GITOPS_TENANT_NAMESPACES),
            "sync_policy": "manual",
            "codecommit": {
                # destroy | retain — what cdk destroy does with the repository.
                "removal_policy": "destroy",
            },
        },
    },
    "ack": {
        "enabled": False,
        "regions": [],
        # ACK service controllers to leave out (ACK's disabledServices).
        "disabled_services": [],
        "enable_cross_namespace": False,
        # IAM roles the capability role may assume (ACK IAM Role Selectors,
        # the documented least-privilege model). Empty grants nothing.
        "assume_role_arns": [],
    },
    "kro": {
        "enabled": False,
        "regions": [],
    },
}


class EksCapabilitiesConfigError(ValueError):
    """Raised when the ``eks_capabilities`` block is malformed."""


def _deep_merge(defaults: Mapping[str, Any], overrides: Mapping[str, Any]) -> dict[str, Any]:
    merged: dict[str, Any] = copy.deepcopy(dict(defaults))
    for key, value in overrides.items():
        if isinstance(value, Mapping) and isinstance(merged.get(key), Mapping):
            merged[key] = _deep_merge(merged[key], value)
        else:
            merged[key] = copy.deepcopy(value)
    return merged


def parse_eks_capabilities_overrides(raw: object) -> dict[str, Any]:
    """Parse the ``eks_capabilities_overrides`` context value into a mapping.

    Accepts a JSON object string (the only shape ``cdk --context`` can carry)
    or a mapping (cdk.json-style); ``None`` and ``""`` mean no overrides. The
    result is validated later as part of the merged block, so this only
    checks the shape.
    """
    if raw is None:
        return {}
    if isinstance(raw, Mapping):
        return copy.deepcopy(dict(raw))
    if isinstance(raw, str):
        if not raw.strip():
            return {}
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise EksCapabilitiesConfigError(
                f"{EKS_CAPABILITIES_OVERRIDES_CONTEXT_KEY} must be a JSON object: {exc}"
            ) from exc
        if not isinstance(parsed, Mapping):
            raise EksCapabilitiesConfigError(
                f"{EKS_CAPABILITIES_OVERRIDES_CONTEXT_KEY} must be a JSON object, got "
                f"{type(parsed).__name__}"
            )
        return dict(parsed)
    raise EksCapabilitiesConfigError(
        f"{EKS_CAPABILITIES_OVERRIDES_CONTEXT_KEY} must be a JSON object string or an object, "
        f"got {type(raw).__name__}"
    )


def merge_eks_capabilities_overrides(raw: object, overrides: Mapping[str, Any]) -> object:
    """Deep-merge run-scoped ``overrides`` over the raw cdk.json block.

    Returns ``raw`` untouched when there is nothing to merge (so an absent
    block stays absent), the overrides alone when the block is absent, and
    the merged mapping otherwise. A non-mapping block is returned as-is so
    validation reports it by name.
    """
    if not overrides:
        return raw
    if raw is None:
        return copy.deepcopy(dict(overrides))
    if not isinstance(raw, Mapping):
        return raw
    return _deep_merge(raw, overrides)


def normalize_eks_capabilities_config(raw: object) -> dict[str, Any]:
    """Return the block with defaults filled in (does not validate).

    ``None`` and ``{}`` both mean "everything off". Unknown keys are kept so
    :func:`validate_eks_capabilities_config` can reject them by name.
    """
    if raw is None:
        return copy.deepcopy(EKS_CAPABILITIES_DEFAULTS)
    if not isinstance(raw, Mapping):
        raise EksCapabilitiesConfigError(
            f"{EKS_CAPABILITIES_CONTEXT_KEY} must be an object, got {type(raw).__name__}"
        )
    return _deep_merge(EKS_CAPABILITIES_DEFAULTS, raw)


def _require_bool(value: object, path: str) -> None:
    if type(value) is not bool:
        raise EksCapabilitiesConfigError(f"{path} must be a boolean, got {value!r}")


def _require_string_list(value: object, path: str, *, allow_empty_items: bool = False) -> list[str]:
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise EksCapabilitiesConfigError(f"{path} must be a list of strings, got {value!r}")
    if not allow_empty_items and any(not item.strip() for item in value):
        raise EksCapabilitiesConfigError(f"{path} must not contain empty strings")
    if len(set(value)) != len(value):
        raise EksCapabilitiesConfigError(f"{path} lists a value twice")
    return value


def _require_arn(value: object, path: str, *, prefix: str = "arn:") -> None:
    if not isinstance(value, str) or not value.startswith(prefix):
        raise EksCapabilitiesConfigError(
            f"{path} must be an ARN starting with {prefix!r}, got {value!r}"
        )


def _reject_unknown_keys(block: Mapping[str, Any], allowed: Iterable[str], path: str) -> None:
    unknown = sorted(str(key) for key in block if key not in set(allowed))
    if unknown:
        raise EksCapabilitiesConfigError(
            f"{path} contains unknown key(s): {', '.join(unknown)}; "
            f"allowed keys: {', '.join(sorted(allowed))}"
        )


def _validate_regions(value: object, path: str, deployment_regions: Iterable[str] | None) -> None:
    regions = _require_string_list(value, path)
    if deployment_regions is None:
        return
    unknown = sorted(set(regions) - set(deployment_regions))
    if unknown:
        raise EksCapabilitiesConfigError(
            f"{path} names region(s) that are not regional deployment regions: {', '.join(unknown)}"
        )


def _validate_rbac_role_mappings(value: object, path: str) -> None:
    if not isinstance(value, list):
        raise EksCapabilitiesConfigError(f"{path} must be a list of role mappings, got {value!r}")
    for index, mapping in enumerate(value):
        item_path = f"{path}[{index}]"
        if not isinstance(mapping, Mapping):
            raise EksCapabilitiesConfigError(
                f"{item_path} must be an object with role and identities, got {mapping!r}"
            )
        _reject_unknown_keys(mapping, ("role", "identities"), item_path)
        role = mapping.get("role")
        if role not in ARGOCD_RBAC_ROLES:
            raise EksCapabilitiesConfigError(
                f"{item_path}.role must be one of {', '.join(ARGOCD_RBAC_ROLES)}, got {role!r}"
            )
        identities = mapping.get("identities")
        if not isinstance(identities, list) or not identities:
            raise EksCapabilitiesConfigError(
                f"{item_path}.identities must be a non-empty list of {{id, type}} objects"
            )
        for id_index, identity in enumerate(identities):
            id_path = f"{item_path}.identities[{id_index}]"
            if not isinstance(identity, Mapping):
                raise EksCapabilitiesConfigError(f"{id_path} must be an object with id and type")
            _reject_unknown_keys(identity, ("id", "type"), id_path)
            identity_id = identity.get("id")
            if not isinstance(identity_id, str) or not identity_id.strip():
                raise EksCapabilitiesConfigError(f"{id_path}.id must be a non-empty string")
            identity_type = identity.get("type")
            if identity_type not in ARGOCD_SSO_IDENTITY_TYPES:
                raise EksCapabilitiesConfigError(
                    f"{id_path}.type must be one of {', '.join(ARGOCD_SSO_IDENTITY_TYPES)}, "
                    f"got {identity_type!r}"
                )


def _validate_gitops(gitops: Mapping[str, Any], argocd_enabled: bool, path: str) -> None:
    _reject_unknown_keys(gitops, EKS_CAPABILITIES_DEFAULTS["argocd"]["gitops"].keys(), path)
    _require_bool(gitops["enabled"], f"{path}.enabled")
    for key in ("source", "repo_url", "revision", "path"):
        if not isinstance(gitops[key], str):
            raise EksCapabilitiesConfigError(f"{path}.{key} must be a string, got {gitops[key]!r}")
    source = gitops["source"]
    if source not in GITOPS_SOURCES:
        raise EksCapabilitiesConfigError(
            f"{path}.source must be one of {', '.join(GITOPS_SOURCES)}, got {source!r}"
        )
    codecommit = gitops["codecommit"]
    if not isinstance(codecommit, Mapping):
        raise EksCapabilitiesConfigError(f"{path}.codecommit must be an object, got {codecommit!r}")
    _reject_unknown_keys(
        codecommit,
        EKS_CAPABILITIES_DEFAULTS["argocd"]["gitops"]["codecommit"].keys(),
        f"{path}.codecommit",
    )
    if codecommit["removal_policy"] not in GITOPS_CODECOMMIT_REMOVAL_POLICIES:
        raise EksCapabilitiesConfigError(
            f"{path}.codecommit.removal_policy must be one of "
            f"{', '.join(GITOPS_CODECOMMIT_REMOVAL_POLICIES)}, got {codecommit['removal_policy']!r}"
        )
    namespaces = _require_string_list(
        gitops["destination_namespaces"], f"{path}.destination_namespaces"
    )
    outside = sorted(set(namespaces) - set(GITOPS_TENANT_NAMESPACES))
    if outside:
        raise EksCapabilitiesConfigError(
            f"{path}.destination_namespaces may only name the tenant namespaces "
            f"{', '.join(GITOPS_TENANT_NAMESPACES)} (GCO ships Argo CD write RBAC for exactly "
            f"those); got {', '.join(outside)}"
        )
    if gitops["sync_policy"] not in GITOPS_SYNC_POLICIES:
        raise EksCapabilitiesConfigError(
            f"{path}.sync_policy must be one of {', '.join(GITOPS_SYNC_POLICIES)}, "
            f"got {gitops['sync_policy']!r}"
        )
    repo_url = gitops["repo_url"].strip()
    if source == "codecommit" and repo_url:
        raise EksCapabilitiesConfigError(
            f"{path}.repo_url applies to source: git only; with source: codecommit GCO creates "
            "and names the repository itself (drop repo_url, or set source: git to use yours)"
        )
    if not gitops["enabled"]:
        return
    if not argocd_enabled:
        raise EksCapabilitiesConfigError(
            f"{path}.enabled requires eks_capabilities.argocd.enabled: true "
            "(the hand-off needs the hosted Argo CD it points at)"
        )
    if source == "git" and not (
        repo_url.startswith(("https://", "ssh://", "git@")) or repo_url.endswith(".git")
    ):
        raise EksCapabilitiesConfigError(
            f"{path}.repo_url must be a Git repository URL (https://, ssh:// or git@) when "
            f"source is git, got {gitops['repo_url']!r}"
        )
    if not gitops["revision"].strip():
        raise EksCapabilitiesConfigError(f"{path}.revision must be a non-empty branch, tag or SHA")
    if not namespaces:
        raise EksCapabilitiesConfigError(f"{path}.destination_namespaces must not be empty")


def validate_eks_capabilities_config(
    raw: object,
    deployment_regions: Iterable[str] | None = None,
) -> dict[str, Any]:
    """Validate the raw cdk.json block and return it normalized.

    ``deployment_regions`` (the regional deployment regions) lets the
    per-type ``regions`` subsets be checked; pass ``None`` to skip that check
    (e.g. when the caller has no region list yet).
    """
    if raw is None:
        return copy.deepcopy(EKS_CAPABILITIES_DEFAULTS)
    if not isinstance(raw, Mapping):
        raise EksCapabilitiesConfigError(
            f"{EKS_CAPABILITIES_CONTEXT_KEY} must be an object, got {type(raw).__name__}"
        )
    _reject_unknown_keys(raw, EKS_CAPABILITY_TYPES, EKS_CAPABILITIES_CONTEXT_KEY)
    for type_name in EKS_CAPABILITY_TYPES:
        block = raw.get(type_name)
        if block is not None and not isinstance(block, Mapping):
            raise EksCapabilitiesConfigError(
                f"{EKS_CAPABILITIES_CONTEXT_KEY}.{type_name} must be an object, got {block!r}"
            )
        if isinstance(block, Mapping):
            _reject_unknown_keys(
                block,
                EKS_CAPABILITIES_DEFAULTS[type_name].keys(),
                f"{EKS_CAPABILITIES_CONTEXT_KEY}.{type_name}",
            )
    config = _deep_merge(EKS_CAPABILITIES_DEFAULTS, raw)
    regions_list = list(deployment_regions) if deployment_regions is not None else None

    for type_name in EKS_CAPABILITY_TYPES:
        block = config[type_name]
        path = f"{EKS_CAPABILITIES_CONTEXT_KEY}.{type_name}"
        _require_bool(block["enabled"], f"{path}.enabled")
        _validate_regions(block["regions"], f"{path}.regions", regions_list)

    argocd = config["argocd"]
    _require_string_list(argocd["vpce_ids"], "eks_capabilities.argocd.vpce_ids")
    for key in ("repo_credentials_secret_arns", "repo_credentials_kms_key_arns"):
        for index, arn in enumerate(
            _require_string_list(argocd[key], f"eks_capabilities.argocd.{key}")
        ):
            _require_arn(arn, f"eks_capabilities.argocd.{key}[{index}]")
    if argocd["repo_credentials_kms_key_arns"] and not argocd["repo_credentials_secret_arns"]:
        raise EksCapabilitiesConfigError(
            "eks_capabilities.argocd.repo_credentials_kms_key_arns needs "
            "repo_credentials_secret_arns: the keys only matter for decrypting those secrets"
        )
    if not isinstance(argocd["idc_instance_arn"], str) or not isinstance(argocd["idc_region"], str):
        raise EksCapabilitiesConfigError(
            "eks_capabilities.argocd.idc_instance_arn and idc_region must be strings"
        )
    _validate_rbac_role_mappings(
        argocd["rbac_role_mappings"], "eks_capabilities.argocd.rbac_role_mappings"
    )
    if argocd["enabled"]:
        # Hosted Argo CD authenticates only through IAM Identity Center, and an
        # instance nobody can sign in to is a billed no-op: fail at synth.
        _require_arn(argocd["idc_instance_arn"], "eks_capabilities.argocd.idc_instance_arn")
        if not argocd["rbac_role_mappings"]:
            raise EksCapabilitiesConfigError(
                "eks_capabilities.argocd.rbac_role_mappings must grant at least one Identity "
                "Center user or group a role when argocd is enabled"
            )
    gitops = argocd["gitops"]
    if not isinstance(gitops, Mapping):
        raise EksCapabilitiesConfigError("eks_capabilities.argocd.gitops must be an object")
    _validate_gitops(gitops, bool(argocd["enabled"]), "eks_capabilities.argocd.gitops")

    ack = config["ack"]
    _require_string_list(ack["disabled_services"], "eks_capabilities.ack.disabled_services")
    _require_bool(ack["enable_cross_namespace"], "eks_capabilities.ack.enable_cross_namespace")
    for index, arn in enumerate(
        _require_string_list(ack["assume_role_arns"], "eks_capabilities.ack.assume_role_arns")
    ):
        _require_arn(arn, f"eks_capabilities.ack.assume_role_arns[{index}]")

    return config


def capability_enabled_in_region(config: Mapping[str, Any], type_name: str, region: str) -> bool:
    """True when ``type_name`` is on and applies to ``region`` (empty regions = all)."""
    block = config.get(type_name)
    if not isinstance(block, Mapping) or block.get("enabled") is not True:
        return False
    regions = block.get("regions") or []
    return not regions or region in regions


def enabled_capability_types(config: Mapping[str, Any], region: str) -> list[str]:
    """The cdk.json type names enabled for ``region``, in canonical order."""
    return [
        name for name in EKS_CAPABILITY_TYPES if capability_enabled_in_region(config, name, region)
    ]


def gitops_enabled_in_region(config: Mapping[str, Any], region: str) -> bool:
    """True when the Argo CD GitOps hand-off applies to ``region``."""
    if not capability_enabled_in_region(config, "argocd", region):
        return False
    gitops = config.get("argocd", {}).get("gitops")
    return isinstance(gitops, Mapping) and gitops.get("enabled") is True


def gitops_source(config: Mapping[str, Any]) -> str:
    """The hand-off's manifest source (``codecommit`` or ``git``) for a normalized block."""
    gitops = config.get("argocd", {}).get("gitops")
    if not isinstance(gitops, Mapping):
        return "codecommit"
    return str(gitops.get("source") or "codecommit")


def gitops_codecommit_enabled_in_region(config: Mapping[str, Any], region: str) -> bool:
    """True when ``region`` gets a GCO-managed CodeCommit repository for the hand-off."""
    return gitops_enabled_in_region(config, region) and gitops_source(config) == "codecommit"


def gitops_codecommit_repository_name(cluster_name: str) -> str:
    """Name of the GCO-managed CodeCommit repository for one cluster (``<cluster>-gitops``).

    CodeCommit repository names are per account per region; the cluster name
    already carries the project prefix and the region, so the inventory
    scanners recognize the repository as project-owned by name.
    """
    return f"{cluster_name}-gitops"


def aws_url_suffix_for_region(region: str) -> str:
    """The partition DNS suffix a region's regional endpoints hang off."""
    return "amazonaws.com.cn" if region.startswith("cn-") else "amazonaws.com"


def codecommit_clone_url_http(
    region: str, repository_name: str, *, url_suffix: str = "amazonaws.com"
) -> str:
    """The HTTPS clone URL the hosted Argo CD reads a CodeCommit repository from.

    ``https://git-codecommit.<region>.<suffix>/v1/repos/<name>`` is the form
    the EKS Argo CD documentation gives for direct (IAM-authenticated)
    CodeCommit sources; ``url_suffix`` may be a CloudFormation token.
    """
    return f"https://git-codecommit.{region}.{url_suffix}/v1/repos/{repository_name}"


def effective_gitops_path(gitops: Mapping[str, Any]) -> str:
    """The repository ``path`` template: the configured one, or the source default."""
    configured = str(gitops.get("path") or "").strip()
    if configured:
        return configured
    return GITOPS_DEFAULT_PATHS[str(gitops.get("source") or "codecommit")]


def render_gitops_path(path_template: str, *, region: str, cluster_name: str) -> str:
    """Substitute the per-cluster placeholders in a GitOps ``path``.

    Plain ``str.replace`` rather than ``str.format`` so any other brace in the
    path (a Kustomize overlay dir named ``{prod}`` for instance) is left alone.
    """
    return path_template.replace("{region}", region).replace("{cluster_name}", cluster_name)


def gitops_repository_url(
    config: Mapping[str, Any],
    *,
    region: str,
    cluster_name: str,
    url_suffix: str = "amazonaws.com",
) -> str:
    """The repository URL the root ``Application`` in ``region`` points at.

    ``source: git`` returns the configured ``repo_url``; ``source: codecommit``
    derives the GCO-managed repository's clone URL from the cluster name, so
    the stack, the CLI and the live harness never disagree about it.
    """
    gitops = config["argocd"]["gitops"]
    if gitops_source(config) == "git":
        return str(gitops["repo_url"]).strip()
    return codecommit_clone_url_http(
        region, gitops_codecommit_repository_name(cluster_name), url_suffix=url_suffix
    )


#: kubectl-applier tokens rendered for the Argo CD capability manifests.
#: ``07-argocd-cluster-access.yaml`` is gated on the role ARN;
#: ``08-argocd-gitops.yaml`` is gated on the repository URL. Both files also
#: read ``{{EKS_CLUSTER_ARN}}``, which the regional stack always supplies.
EKS_CAPABILITIES_MANIFEST_TOKENS: tuple[str, ...] = (
    "{{ARGOCD_CAPABILITY_ROLE_ARN}}",
    "{{ARGOCD_GITOPS_REPO_URL}}",
    "{{ARGOCD_GITOPS_REVISION}}",
    "{{ARGOCD_GITOPS_PATH}}",
    "{{ARGOCD_GITOPS_DEFAULT_NAMESPACE}}",
    "{{ARGOCD_GITOPS_DESTINATIONS}}",
    "{{ARGOCD_GITOPS_SYNC_POLICY}}",
)


def compute_eks_capabilities_replacements(
    capabilities_config: Mapping[str, Any],
    *,
    region: str,
    cluster_name: str,
    cluster_arn: str,
    argocd_role_arn: str | None,
    url_suffix: str = "amazonaws.com",
) -> dict[str, str]:
    """Build the kubectl-applier replacements for the Argo CD capability manifests.

    Shared by the regional stack (deploy), the kind CI job and the
    live-validation harness so all three render the two manifests identically.
    Two manifests hang off it:

    * ``07-argocd-cluster-access.yaml`` (local-cluster registration Secret and
      the capability role's Kubernetes RBAC) resolves only when
      ``{{ARGOCD_CAPABILITY_ROLE_ARN}}`` is present, i.e. the Argo CD
      capability is enabled for this region.
    * ``08-argocd-gitops.yaml`` (the fenced ``AppProject`` and the root
      ``Application``) additionally needs the ``{{ARGOCD_GITOPS_*}}`` tokens,
      present only when the GitOps hand-off is enabled.

    An absent key is the off switch: the applier gates a manifest out of the
    plan while any ``{{TOKEN}}`` in it stays unresolved and prunes the
    inventory registered for that token, exactly like the FSx, Valkey and
    observability gates. The two list-valued tokens render as single-line JSON
    (valid YAML flow style) so they are indentation-independent; the Argo CD
    destinations use the EKS cluster ARN because the hosted capability
    identifies clusters by ARN, not by API-server URL. ``cluster_arn`` and
    ``url_suffix`` (the partition DNS suffix a ``source: codecommit``
    repository URL is built from) may be CloudFormation tokens: they are only
    ever concatenated into strings.
    """
    if argocd_role_arn is None:
        return {}
    replacements: dict[str, str] = {"{{ARGOCD_CAPABILITY_ROLE_ARN}}": argocd_role_arn}
    if not gitops_enabled_in_region(capabilities_config, region):
        return replacements

    gitops = capabilities_config["argocd"]["gitops"]
    namespaces = [str(namespace) for namespace in gitops["destination_namespaces"]]
    destinations = [{"server": cluster_arn, "namespace": namespace} for namespace in namespaces]
    if gitops["sync_policy"] == "automated":
        # Self-heal reverts drift back to Git; prune stays off so a bad commit
        # cannot delete tenant workloads — operators opt into that in-repo.
        sync_policy: dict[str, Any] = {"automated": {"selfHeal": True, "prune": False}}
    else:
        sync_policy = {}
    replacements.update(
        {
            "{{ARGOCD_GITOPS_REPO_URL}}": gitops_repository_url(
                capabilities_config,
                region=region,
                cluster_name=cluster_name,
                url_suffix=url_suffix,
            ),
            "{{ARGOCD_GITOPS_REVISION}}": str(gitops["revision"]).strip(),
            "{{ARGOCD_GITOPS_PATH}}": render_gitops_path(
                effective_gitops_path(gitops), region=region, cluster_name=cluster_name
            ),
            "{{ARGOCD_GITOPS_DEFAULT_NAMESPACE}}": namespaces[0],
            "{{ARGOCD_GITOPS_DESTINATIONS}}": json.dumps(destinations),
            "{{ARGOCD_GITOPS_SYNC_POLICY}}": json.dumps(sync_policy),
        }
    )
    return replacements
