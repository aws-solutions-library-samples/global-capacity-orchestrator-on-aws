"""Schema, defaults and validation for the ``eks_capabilities`` cdk.json block.

EKS Capabilities are AWS-managed installations of AWS Controllers for
Kubernetes (ACK) and kro that run in an AWS-owned account and attach to a
cluster as ``AWS::EKS::Capability`` resources. GCO exposes them as opt-in
per-type knobs: every type is **off by default**, and each enabled type
synthesizes one capability IAM role and one capability per selected regional
cluster. Argo CD is deliberately not offered here: GCO installs and runs it
in the cluster itself (``helm.argocd``, see ``gco.argocd_config``).

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
import re
from collections.abc import Iterable, Mapping
from typing import Any

#: cdk.json context key.
EKS_CAPABILITIES_CONTEXT_KEY = "eks_capabilities"

#: Run-scoped CDK context key carrying a JSON object deep-merged over the
#: cdk.json block for one deploy (``cdk deploy --context
#: eks_capabilities_overrides='{"kro": {"enabled": true}}'``). The
#: capabilities sibling of ``helm_enabled_overrides`` / ``feature_enabled_overrides``:
#: the live-validation and example harnesses, whose preflight requires a clean
#: worktree, enable capabilities for one run this way.
EKS_CAPABILITIES_OVERRIDES_CONTEXT_KEY = "eks_capabilities_overrides"

#: Capability types GCO can attach, keyed the way cdk.json spells them.
EKS_CAPABILITY_TYPES: tuple[str, ...] = ("ack", "kro")

#: cdk.json spelling -> ``AWS::EKS::Capability`` ``Type`` value.
CAPABILITY_TYPE_API_NAMES: dict[str, str] = {"ack": "ACK", "kro": "KRO"}

#: Session name the EKS kro capability sets when it assumes its role. The
#: capability's Kubernetes user is therefore
#: ``arn:<partition>:sts::<account>:assumed-role/<role name>/KRO`` — the
#: subject GCO's tenant RBAC for kro binds (AWS docs, "Security
#: considerations for EKS Capabilities").
KRO_SESSION_NAME = "KRO"

#: A managed IAM policy ARN: an AWS managed policy (``iam::aws:policy/...``)
#: or a customer managed one in a 12-digit account.
_POLICY_ARN_RE = re.compile(r"arn:aws[a-z-]*:iam::(aws|\d{12}):policy/[\w+=,.@/-]+")

#: The full block with every knob at its default. ``regions: []`` means every
#: regional deployment region; a non-empty list restricts the type to that
#: subset of clusters.
EKS_CAPABILITIES_DEFAULTS: dict[str, Any] = {
    "ack": {
        "enabled": False,
        "regions": [],
        # ACK service controllers to leave out (ACK's disabledServices).
        "disabled_services": [],
        "enable_cross_namespace": False,
        # IAM roles the capability role may assume (ACK IAM Role Selectors,
        # the documented least-privilege model). Empty grants nothing.
        "assume_role_arns": [],
        # Managed IAM policies attached to the capability role itself (the
        # documented "simple permission setup"). Empty grants nothing: ACK
        # can call no AWS API until one of these two lists says otherwise.
        "iam_policy_arns": [],
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


def _require_string_list(value: object, path: str) -> list[str]:
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise EksCapabilitiesConfigError(f"{path} must be a list of strings, got {value!r}")
    if any(not item.strip() for item in value):
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

    ack = config["ack"]
    _require_string_list(ack["disabled_services"], "eks_capabilities.ack.disabled_services")
    _require_bool(ack["enable_cross_namespace"], "eks_capabilities.ack.enable_cross_namespace")
    for index, arn in enumerate(
        _require_string_list(ack["assume_role_arns"], "eks_capabilities.ack.assume_role_arns")
    ):
        _require_arn(arn, f"eks_capabilities.ack.assume_role_arns[{index}]")
    for index, arn in enumerate(
        _require_string_list(ack["iam_policy_arns"], "eks_capabilities.ack.iam_policy_arns")
    ):
        if not _POLICY_ARN_RE.fullmatch(arn):
            raise EksCapabilitiesConfigError(
                f"eks_capabilities.ack.iam_policy_arns[{index}] must be a managed IAM policy "
                f"ARN (arn:<partition>:iam::aws:policy/... or arn:<partition>:iam::"
                f"<account>:policy/...), got {arn!r}"
            )

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


def kro_kubernetes_username(*, partition: str, account: str, role_name: str) -> str:
    """The Kubernetes user the EKS kro capability acts as.

    Every argument may be a CloudFormation token (the stack passes its
    partition, account and the generated role name); they are only ever
    concatenated.
    """
    return f"arn:{partition}:sts::{account}:assumed-role/{role_name}/{KRO_SESSION_NAME}"


#: kubectl-applier tokens rendered for the capability manifests.
#: ``07-kro-tenant-access.yaml`` is gated on the kro user name.
EKS_CAPABILITIES_MANIFEST_TOKENS: tuple[str, ...] = ("{{KRO_CAPABILITY_USERNAME}}",)


def compute_eks_capabilities_replacements(*, kro_username: str | None) -> dict[str, str]:
    """Build the kubectl-applier replacements for the capability manifests.

    Shared by the regional stack (deploy) and the tests so both render
    ``07-kro-tenant-access.yaml`` identically. The file resolves only when
    ``kro_username`` is given, i.e. the kro capability is enabled for this
    region. An absent key is the off switch: the applier gates the manifest
    out of the plan while its token stays unresolved and prunes the inventory
    registered for that token, exactly like the FSx, Valkey and observability
    gates.
    """
    if kro_username is None:
        return {}
    return {"{{KRO_CAPABILITY_USERNAME}}": kro_username}
