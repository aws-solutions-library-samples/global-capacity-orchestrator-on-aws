"""EKS Capabilities (AWS-managed Argo CD / ACK / kro) and the Argo CD GitOps hand-off.

Three layers, one contract:

* ``gco/config/eks_capabilities.py`` — the ``eks_capabilities`` cdk.json block:
  every type off by default, per-type ``regions`` subsets, Argo CD's Identity
  Center + RBAC prerequisites, the GitOps sub-block's fence (tenant
  namespaces only), and the loader wrapping its errors as
  ``ConfigValidationError``.
* ``gco/stacks/regional_stack.py`` — one capability IAM role (trusted by
  ``capabilities.eks.amazonaws.com`` with ``sts:AssumeRole`` +
  ``sts:TagSession``) and one ``AWS::EKS::Capability`` per enabled type, the
  outputs, the convergence trigger's dependency on every capability, and the
  kubectl-applier tokens that gate ``07-argocd-cluster-access.yaml`` /
  ``08-argocd-gitops.yaml``.
* The shipped default synthesizes exactly today's template: no capability
  resources and no ``{{ARGOCD_*}}`` tokens.

Synthesis is expensive, so the three stack shapes (all off, everything on,
a per-region subset) are synthesized once per module and shared.
"""

from __future__ import annotations

import copy
import json
import re
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import aws_cdk as cdk
import pytest
from aws_cdk import assertions

from gco import eks_capabilities_config as caps
from gco.config.config_loader import ConfigLoader, ConfigValidationError
from gco.stacks.regional_stack import GCORegionalStack
from tests.test_regional_stack import MockConfigLoader
from tests.test_regional_stack import TestRegionalStackSynthesis as _RegionalStackSynthesisFixtures

_ACCOUNT = "123456789012"
_REGION = "us-east-1"
_OTHER_REGION = "us-west-2"
_IDC_ARN = "arn:aws:sso:::instance/ssoins-1234567890abcdef"
_SECRET_ARN = f"arn:aws:secretsmanager:{_REGION}:{_ACCOUNT}:secret:gco/git-creds-AbCdEf"
_SECRET_ARN_PATTERN = f"arn:aws:secretsmanager:{_REGION}:{_ACCOUNT}:secret:gco/git-creds-*"
_KMS_KEY_ARN = f"arn:aws:kms:{_REGION}:{_ACCOUNT}:key/11111111-2222-3333-4444-555555555555"
_ACK_TARGET_ROLE = f"arn:aws:iam::{_ACCOUNT}:role/ack-s3-controller"
_ACK_TARGET_PATTERN = f"arn:aws:iam::{_ACCOUNT}:role/ack-*"
_REPO_URL = "https://github.com/example/gco-tenants.git"
# This repository, which the kind CI job points the root Application at (one
# line, so scripts/migrate_fork.py can classify and rewrite it on a fork).
_CI_REPO = "https://github.com/aws-solutions-library-samples/global-capacity-orchestrator-on-aws"
_MANIFESTS_DIR = Path(__file__).resolve().parent.parent / "lambda/kubectl-applier-simple/manifests"

_ADMIN_MAPPING = {"role": "ADMIN", "identities": [{"id": "u-admin", "type": "SSO_USER"}]}
_VIEWER_MAPPING = {"role": "VIEWER", "identities": [{"id": "g-viewers", "type": "SSO_GROUP"}]}


def _argocd(**overrides: Any) -> dict[str, Any]:
    """A minimal valid enabled Argo CD block (Identity Center + one mapping)."""
    block: dict[str, Any] = {
        "enabled": True,
        "idc_instance_arn": _IDC_ARN,
        "rbac_role_mappings": [_ADMIN_MAPPING],
    }
    block.update(overrides)
    return block


def _gitops(**overrides: Any) -> dict[str, Any]:
    """An enabled hand-off pointed at an operator-owned repository (``source: git``)."""
    block: dict[str, Any] = {"enabled": True, "source": "git", "repo_url": _REPO_URL}
    block.update(overrides)
    return block


def _gitops_codecommit(**overrides: Any) -> dict[str, Any]:
    """An enabled hand-off on the GCO-managed CodeCommit repository (the default source)."""
    block: dict[str, Any] = {"enabled": True}
    block.update(overrides)
    return block


# ─── gco/config/eks_capabilities.py ──────────────────────────────────────────


class TestDefaultsAndNormalization:
    def test_every_type_is_off_by_default(self) -> None:
        for type_name in caps.EKS_CAPABILITY_TYPES:
            block = caps.EKS_CAPABILITIES_DEFAULTS[type_name]
            assert block["enabled"] is False
            assert block["regions"] == []
        assert caps.EKS_CAPABILITIES_DEFAULTS["argocd"]["gitops"]["enabled"] is False

    def test_type_names_map_to_the_api_enum(self) -> None:
        assert caps.CAPABILITY_TYPE_API_NAMES == {"argocd": "ARGOCD", "ack": "ACK", "kro": "KRO"}
        assert set(caps.CAPABILITY_TYPE_API_NAMES) == set(caps.EKS_CAPABILITY_TYPES)

    @pytest.mark.parametrize("raw", [None, {}])
    def test_absent_or_empty_block_normalizes_to_the_defaults(self, raw: object) -> None:
        normalized = caps.normalize_eks_capabilities_config(raw)
        assert normalized == caps.EKS_CAPABILITIES_DEFAULTS
        assert normalized is not caps.EKS_CAPABILITIES_DEFAULTS  # a copy, never the constant

    def test_normalize_deep_merges_nested_blocks(self) -> None:
        normalized = caps.normalize_eks_capabilities_config(
            {"argocd": {"enabled": True, "gitops": {"source": "git", "repo_url": _REPO_URL}}}
        )
        assert normalized["argocd"]["enabled"] is True
        assert normalized["argocd"]["gitops"]["repo_url"] == _REPO_URL
        assert normalized["argocd"]["gitops"]["revision"] == "HEAD"
        # An empty path means "the source's default" (resolved at render time).
        assert normalized["argocd"]["gitops"]["path"] == ""
        assert normalized["argocd"]["gitops"]["codecommit"] == {"removal_policy": "destroy"}
        assert normalized["ack"] == caps.EKS_CAPABILITIES_DEFAULTS["ack"]

    def test_codecommit_is_the_default_source(self) -> None:
        # Batteries included: `gitops: {enabled: true}` alone gives every
        # selected cluster its own GCO-managed CodeCommit repository.
        defaults = caps.EKS_CAPABILITIES_DEFAULTS["argocd"]["gitops"]
        assert defaults["source"] == "codecommit"
        assert defaults["repo_url"] == ""
        assert caps.GITOPS_SOURCES == ("codecommit", "git")
        assert caps.GITOPS_DEFAULT_PATHS == {"codecommit": ".", "git": "clusters/{region}"}
        assert caps.GITOPS_CODECOMMIT_DEFAULT_BRANCH == "main"

    def test_effective_gitops_path_falls_back_per_source(self) -> None:
        assert caps.effective_gitops_path({"source": "codecommit", "path": ""}) == "."
        assert caps.effective_gitops_path({"source": "git", "path": " "}) == "clusters/{region}"
        assert caps.effective_gitops_path({"source": "codecommit", "path": " apps "}) == "apps"
        # A block that predates the source knob reads as the default source.
        assert caps.effective_gitops_path({"path": ""}) == "."

    def test_normalize_does_not_mutate_the_defaults(self) -> None:
        before = copy.deepcopy(caps.EKS_CAPABILITIES_DEFAULTS)
        normalized = caps.normalize_eks_capabilities_config({"kro": {"enabled": True}})
        normalized["kro"]["regions"].append("eu-west-1")
        assert before == caps.EKS_CAPABILITIES_DEFAULTS

    def test_non_object_block_is_rejected(self) -> None:
        with pytest.raises(caps.EksCapabilitiesConfigError, match="must be an object"):
            caps.normalize_eks_capabilities_config(["argocd"])

    def test_validate_none_returns_the_defaults(self) -> None:
        assert caps.validate_eks_capabilities_config(None) == caps.EKS_CAPABILITIES_DEFAULTS

    def test_tenant_namespaces_are_the_two_gco_ships_rbac_for(self) -> None:
        # 07-argocd-cluster-access.yaml grants Argo CD write RBAC in exactly
        # these namespaces; the fence must not name any other.
        assert caps.GITOPS_TENANT_NAMESPACES == ("gco-jobs", "gco-inference")
        assert "gco-system" not in caps.GITOPS_TENANT_NAMESPACES
        assert caps.EKS_CAPABILITIES_DEFAULTS["argocd"]["gitops"]["destination_namespaces"] == list(
            caps.GITOPS_TENANT_NAMESPACES
        )


class TestValidationRejects:
    """Every malformed shape fails loudly with a path-qualified message."""

    def _reject(self, raw: dict[str, Any], match: str, regions: list[str] | None = None) -> None:
        with pytest.raises(caps.EksCapabilitiesConfigError, match=match):
            caps.validate_eks_capabilities_config(raw, regions)

    def test_unknown_top_level_key(self) -> None:
        self._reject(
            {"flux": {"enabled": True}}, r"eks_capabilities contains unknown key\(s\): flux"
        )

    def test_unknown_type_key(self) -> None:
        self._reject(
            {"kro": {"enabled": True, "version": "1"}}, r"eks_capabilities\.kro contains unknown"
        )

    def test_type_block_must_be_an_object(self) -> None:
        self._reject({"kro": True}, r"eks_capabilities\.kro must be an object")

    @pytest.mark.parametrize("value", ["true", 1, None])
    def test_enabled_must_be_a_real_boolean(self, value: object) -> None:
        self._reject(
            {"kro": {"enabled": value}}, r"eks_capabilities\.kro\.enabled must be a boolean"
        )

    def test_regions_must_be_strings(self) -> None:
        self._reject(
            {"kro": {"regions": [1]}}, r"eks_capabilities\.kro\.regions must be a list of strings"
        )

    def test_regions_must_be_deployment_regions(self) -> None:
        self._reject(
            {"kro": {"enabled": True, "regions": ["eu-west-1"]}},
            r"eks_capabilities\.kro\.regions names region\(s\) that are not regional deployment regions: eu-west-1",
            regions=[_REGION, _OTHER_REGION],
        )

    def test_regions_are_not_checked_without_a_deployment_region_list(self) -> None:
        config = caps.validate_eks_capabilities_config(
            {"kro": {"enabled": True, "regions": ["eu-west-1"]}}
        )
        assert config["kro"]["regions"] == ["eu-west-1"]

    def test_duplicate_regions(self) -> None:
        self._reject(
            {"kro": {"regions": [_REGION, _REGION]}},
            r"eks_capabilities\.kro\.regions lists a value twice",
        )

    def test_argocd_enabled_requires_an_identity_center_instance(self) -> None:
        self._reject(
            {"argocd": {"enabled": True, "rbac_role_mappings": [_ADMIN_MAPPING]}},
            r"eks_capabilities\.argocd\.idc_instance_arn must be an ARN",
        )

    def test_argocd_enabled_requires_at_least_one_rbac_mapping(self) -> None:
        self._reject(
            {"argocd": {"enabled": True, "idc_instance_arn": _IDC_ARN}},
            r"rbac_role_mappings must grant at least one Identity Center user or group",
        )

    def test_argocd_disabled_needs_neither(self) -> None:
        config = caps.validate_eks_capabilities_config({"argocd": {"enabled": False}})
        assert config["argocd"]["idc_instance_arn"] == ""
        assert config["argocd"]["rbac_role_mappings"] == []

    @pytest.mark.parametrize(
        ("mapping", "match"),
        [
            (
                {"role": "OWNER", "identities": [{"id": "u", "type": "SSO_USER"}]},
                r"role must be one of ADMIN, EDITOR, VIEWER",
            ),
            ({"role": "ADMIN", "identities": []}, r"identities must be a non-empty list"),
            ({"role": "ADMIN"}, r"identities must be a non-empty list"),
            (
                {"role": "ADMIN", "identities": [{"id": "", "type": "SSO_USER"}]},
                r"identities\[0\]\.id must be a non-empty string",
            ),
            (
                {"role": "ADMIN", "identities": [{"id": "u", "type": "USER"}]},
                r"type must be one of SSO_USER, SSO_GROUP",
            ),
            (
                {"role": "ADMIN", "identities": [{"id": "u", "type": "SSO_USER", "email": "x"}]},
                r"identities\[0\] contains unknown key\(s\): email",
            ),
            (
                {"role": "ADMIN", "identities": [{"id": "u", "type": "SSO_USER"}], "scope": "x"},
                r"rbac_role_mappings\[0\] contains unknown key\(s\): scope",
            ),
            ("ADMIN", r"rbac_role_mappings\[0\] must be an object"),
        ],
    )
    def test_rbac_role_mapping_shapes(self, mapping: object, match: str) -> None:
        self._reject({"argocd": _argocd(rbac_role_mappings=[mapping])}, match)

    def test_rbac_mappings_are_validated_even_when_argocd_is_off(self) -> None:
        self._reject(
            {"argocd": {"enabled": False, "rbac_role_mappings": "ADMIN"}},
            r"rbac_role_mappings must be a list of role mappings",
        )

    def test_repo_credential_secrets_must_be_arns(self) -> None:
        self._reject(
            {"argocd": {"repo_credentials_secret_arns": ["gco/git-creds"]}},
            r"repo_credentials_secret_arns\[0\] must be an ARN starting with 'arn:'",
        )

    def test_repo_credential_kms_keys_must_be_arns_and_accompany_secrets(self) -> None:
        self._reject(
            {"argocd": {"repo_credentials_kms_key_arns": ["alias/argocd"]}},
            r"repo_credentials_kms_key_arns\[0\] must be an ARN starting with 'arn:'",
        )
        self._reject(
            {"argocd": {"repo_credentials_kms_key_arns": [_KMS_KEY_ARN]}},
            r"repo_credentials_kms_key_arns needs repo_credentials_secret_arns",
        )

    def test_vpce_ids_must_be_strings(self) -> None:
        self._reject({"argocd": {"vpce_ids": [42]}}, r"vpce_ids must be a list of strings")

    def test_idc_fields_must_be_strings(self) -> None:
        self._reject(
            {"argocd": {"idc_region": 1}}, r"idc_instance_arn and idc_region must be strings"
        )

    def test_gitops_unknown_key(self) -> None:
        self._reject(
            {"argocd": _argocd(gitops={"branch": "main"})},
            r"eks_capabilities\.argocd\.gitops contains unknown key\(s\): branch",
        )

    def test_gitops_requires_argocd(self) -> None:
        self._reject(
            {"argocd": {"enabled": False, "gitops": _gitops()}},
            r"gitops\.enabled requires eks_capabilities\.argocd\.enabled: true",
        )

    @pytest.mark.parametrize("repo_url", ["", "github.com/example/repo", "s3://bucket/repo"])
    def test_gitops_requires_a_git_repository_url(self, repo_url: str) -> None:
        self._reject(
            {"argocd": _argocd(gitops=_gitops(repo_url=repo_url))},
            r"gitops\.repo_url must be a Git repository URL",
        )

    @pytest.mark.parametrize(
        "repo_url",
        [
            _REPO_URL,
            "ssh://git@github.com/example/repo.git",
            "git@github.com:example/repo.git",
            "https://git-codecommit.us-east-1.amazonaws.com/v1/repos/tenants",
        ],
    )
    def test_gitops_accepts_https_ssh_and_scp_style_urls(self, repo_url: str) -> None:
        config = caps.validate_eks_capabilities_config(
            {"argocd": _argocd(gitops=_gitops(repo_url=repo_url))}
        )
        assert config["argocd"]["gitops"]["repo_url"] == repo_url

    def test_gitops_revision_must_not_be_blank(self) -> None:
        self._reject(
            {"argocd": _argocd(gitops=_gitops(revision=" "))},
            r"gitops\.revision must be a non-empty",
        )

    def test_gitops_blank_path_is_the_source_default(self) -> None:
        for gitops in (_gitops(path=""), _gitops_codecommit(path=" ")):
            config = caps.validate_eks_capabilities_config({"argocd": _argocd(gitops=gitops)})
            assert config["argocd"]["gitops"]["path"] == gitops["path"]

    def test_gitops_source_enum(self) -> None:
        self._reject(
            {"argocd": _argocd(gitops=_gitops_codecommit(source="gitea"))},
            r"gitops\.source must be one of codecommit, git, got 'gitea'",
        )
        self._reject({"argocd": {"gitops": {"source": 1}}}, r"gitops\.source must be a string")

    def test_codecommit_source_refuses_a_repo_url(self) -> None:
        # The most likely confusion: keeping repo_url while leaving the default
        # source. Say what GCO does instead of silently ignoring the URL.
        self._reject(
            {"argocd": _argocd(gitops=_gitops_codecommit(repo_url=_REPO_URL))},
            r"gitops\.repo_url applies to source: git only.*GCO creates and names the repository",
        )
        # ...even when the hand-off is disabled, so the block never carries a
        # dead setting.
        self._reject(
            {"argocd": {"gitops": {"repo_url": _REPO_URL}}},
            r"gitops\.repo_url applies to source: git only",
        )

    def test_codecommit_block_shapes(self) -> None:
        self._reject(
            {"argocd": {"gitops": {"codecommit": {"removal_policy": "keep"}}}},
            r"codecommit\.removal_policy must be one of destroy, retain, got 'keep'",
        )
        self._reject(
            {"argocd": {"gitops": {"codecommit": {"branch": "main"}}}},
            r"gitops\.codecommit contains unknown key\(s\): branch",
        )
        self._reject(
            {"argocd": {"gitops": {"codecommit": "destroy"}}},
            r"gitops\.codecommit must be an object",
        )
        config = caps.validate_eks_capabilities_config(
            {"argocd": _argocd(gitops=_gitops_codecommit(codecommit={"removal_policy": "retain"}))}
        )
        assert config["argocd"]["gitops"]["codecommit"]["removal_policy"] == "retain"

    def test_gitops_destination_namespaces_are_fenced_to_the_tenant_namespaces(self) -> None:
        self._reject(
            {"argocd": _argocd(gitops=_gitops(destination_namespaces=["gco-jobs", "gco-system"]))},
            r"destination_namespaces may only name the tenant namespaces gco-jobs, gco-inference .*got gco-system",
        )

    def test_gitops_destination_namespaces_fence_applies_even_when_disabled(self) -> None:
        self._reject(
            {"argocd": {"gitops": {"destination_namespaces": ["kube-system"]}}},
            r"destination_namespaces may only name the tenant namespaces",
        )

    def test_gitops_enabled_needs_at_least_one_destination(self) -> None:
        self._reject(
            {"argocd": _argocd(gitops=_gitops(destination_namespaces=[]))},
            r"destination_namespaces must not be empty",
        )

    def test_gitops_sync_policy_enum(self) -> None:
        self._reject(
            {"argocd": _argocd(gitops=_gitops(sync_policy="auto"))},
            r"sync_policy must be one of manual, automated, got 'auto'",
        )

    def test_gitops_must_be_an_object(self) -> None:
        self._reject({"argocd": {"gitops": []}}, r"eks_capabilities\.argocd\.gitops")

    def test_ack_shapes(self) -> None:
        self._reject(
            {"ack": {"disabled_services": "ec2"}},
            r"ack\.disabled_services must be a list of strings",
        )
        self._reject(
            {"ack": {"enable_cross_namespace": "yes"}},
            r"ack\.enable_cross_namespace must be a boolean",
        )
        self._reject(
            {"ack": {"assume_role_arns": ["ack-role"]}},
            r"ack\.assume_role_arns\[0\] must be an ARN",
        )


class TestValidationAccepts:
    def test_full_block_round_trips_with_defaults_filled(self) -> None:
        raw = {
            "argocd": _argocd(
                regions=[_REGION],
                idc_region="us-east-2",
                rbac_role_mappings=[_ADMIN_MAPPING, _VIEWER_MAPPING],
                vpce_ids=["vpce-0123456789abcdef0"],
                repo_credentials_secret_arns=[_SECRET_ARN],
                gitops=_gitops(
                    revision="main",
                    path="clusters/{region}/{cluster_name}",
                    destination_namespaces=["gco-jobs"],
                    sync_policy="automated",
                ),
            ),
            "ack": {
                "enabled": True,
                "disabled_services": ["ec2"],
                "enable_cross_namespace": True,
                "assume_role_arns": [_ACK_TARGET_ROLE],
            },
            "kro": {"enabled": True, "regions": [_OTHER_REGION]},
        }
        config = caps.validate_eks_capabilities_config(raw, [_REGION, _OTHER_REGION])
        assert config["argocd"]["gitops"]["destination_namespaces"] == ["gco-jobs"]
        assert config["argocd"]["gitops"]["sync_policy"] == "automated"
        assert config["ack"]["assume_role_arns"] == [_ACK_TARGET_ROLE]
        assert config["kro"] == {"enabled": True, "regions": [_OTHER_REGION]}
        # Validation never mutates the caller's block.
        assert raw["kro"] == {"enabled": True, "regions": [_OTHER_REGION]}


class TestRegionHelpers:
    _CONFIG = caps.normalize_eks_capabilities_config(
        {
            "argocd": _argocd(regions=[_REGION], gitops=_gitops()),
            "ack": {"enabled": True},  # every region
            "kro": {"enabled": False, "regions": [_REGION]},  # named but off
        }
    )

    def test_empty_regions_means_every_region(self) -> None:
        assert caps.capability_enabled_in_region(self._CONFIG, "ack", _REGION)
        assert caps.capability_enabled_in_region(self._CONFIG, "ack", "eu-west-1")

    def test_named_regions_restrict(self) -> None:
        assert caps.capability_enabled_in_region(self._CONFIG, "argocd", _REGION)
        assert not caps.capability_enabled_in_region(self._CONFIG, "argocd", _OTHER_REGION)

    def test_disabled_type_is_off_everywhere_even_when_regions_name_it(self) -> None:
        assert not caps.capability_enabled_in_region(self._CONFIG, "kro", _REGION)

    def test_enabled_types_keep_canonical_order(self) -> None:
        assert caps.enabled_capability_types(self._CONFIG, _REGION) == ["argocd", "ack"]
        assert caps.enabled_capability_types(self._CONFIG, _OTHER_REGION) == ["ack"]
        assert caps.enabled_capability_types(caps.EKS_CAPABILITIES_DEFAULTS, _REGION) == []

    def test_gitops_follows_the_argocd_region_subset(self) -> None:
        assert caps.gitops_enabled_in_region(self._CONFIG, _REGION)
        assert not caps.gitops_enabled_in_region(self._CONFIG, _OTHER_REGION)
        off = caps.normalize_eks_capabilities_config({"argocd": _argocd()})
        assert not caps.gitops_enabled_in_region(off, _REGION)

    def test_helpers_tolerate_a_malformed_block(self) -> None:
        assert not caps.capability_enabled_in_region({"argocd": "yes"}, "argocd", _REGION)
        assert not caps.capability_enabled_in_region(
            {"argocd": {"enabled": "true"}}, "argocd", _REGION
        )
        assert not caps.gitops_enabled_in_region(
            {"argocd": {"enabled": True, "gitops": []}}, _REGION
        )

    def test_render_gitops_path_substitutes_only_the_two_placeholders(self) -> None:
        rendered = caps.render_gitops_path(
            "clusters/{region}/{cluster_name}/{prod}", region=_REGION, cluster_name="gco-us-east-1"
        )
        assert rendered == "clusters/us-east-1/gco-us-east-1/{prod}"
        assert caps.render_gitops_path("apps", region=_REGION, cluster_name="x") == "apps"

    def test_codecommit_source_helpers(self) -> None:
        codecommit_config = caps.normalize_eks_capabilities_config(
            {"argocd": _argocd(regions=[_REGION], gitops=_gitops_codecommit())}
        )
        assert caps.gitops_source(codecommit_config) == "codecommit"
        assert caps.gitops_source(self._CONFIG) == "git"
        assert caps.gitops_source({}) == "codecommit"
        assert caps.gitops_codecommit_enabled_in_region(codecommit_config, _REGION)
        # Region subset and source both gate the repository.
        assert not caps.gitops_codecommit_enabled_in_region(codecommit_config, _OTHER_REGION)
        assert not caps.gitops_codecommit_enabled_in_region(self._CONFIG, _REGION)
        assert caps.gitops_codecommit_repository_name("gco-us-east-1") == "gco-us-east-1-gitops"
        assert (
            caps.codecommit_clone_url_http("us-east-1", "gco-us-east-1-gitops")
            == "https://git-codecommit.us-east-1.amazonaws.com/v1/repos/gco-us-east-1-gitops"
        )
        assert caps.aws_url_suffix_for_region("cn-north-1") == "amazonaws.com.cn"
        assert caps.aws_url_suffix_for_region("us-gov-west-1") == "amazonaws.com"

    def test_gitops_repository_url_follows_the_source(self) -> None:
        assert (
            caps.gitops_repository_url(self._CONFIG, region=_REGION, cluster_name="gco-us-east-1")
            == _REPO_URL
        )
        codecommit_config = caps.normalize_eks_capabilities_config(
            {"argocd": _argocd(gitops=_gitops_codecommit())}
        )
        assert (
            caps.gitops_repository_url(
                codecommit_config,
                region=_REGION,
                cluster_name="gco-us-east-1",
                url_suffix="${AWS::URLSuffix}",
            )
            == "https://git-codecommit.us-east-1.${AWS::URLSuffix}/v1/repos/gco-us-east-1-gitops"
        )


# ─── ConfigLoader wiring ─────────────────────────────────────────────────────


def _loader(valid_cdk_context: dict[str, Any], block: object = None) -> ConfigLoader:
    context = dict(valid_cdk_context)
    if block is not None:
        context[caps.EKS_CAPABILITIES_CONTEXT_KEY] = block
    return ConfigLoader(cdk.App(context=context))


class TestConfigLoaderWiring:
    def test_absent_block_is_valid_and_reads_as_all_off(self, valid_cdk_context) -> None:
        loader = _loader(valid_cdk_context)
        assert loader.get_eks_capabilities_config() == caps.EKS_CAPABILITIES_DEFAULTS

    def test_partial_block_merges_defaults(self, valid_cdk_context) -> None:
        loader = _loader(valid_cdk_context, {"kro": {"enabled": True}})
        config = loader.get_eks_capabilities_config()
        assert config["kro"] == {"enabled": True, "regions": []}
        assert config["argocd"]["enabled"] is False

    def test_shipped_cdk_json_has_the_block_with_everything_off(self) -> None:
        cdk_json = json.loads((Path(__file__).resolve().parent.parent / "cdk.json").read_text())
        block = cdk_json["context"][caps.EKS_CAPABILITIES_CONTEXT_KEY]
        config = caps.validate_eks_capabilities_config(
            block, cdk_json["context"]["deployment_regions"]["regional"]
        )
        assert config == caps.EKS_CAPABILITIES_DEFAULTS
        assert "_comment_eks_capabilities" in cdk_json["context"]

    def test_malformed_block_fails_at_construction_as_config_validation_error(
        self, valid_cdk_context
    ) -> None:
        with pytest.raises(
            ConfigValidationError,
            match=r"eks_capabilities\.argocd\.idc_instance_arn must be an ARN",
        ):
            _loader(
                valid_cdk_context,
                {"argocd": {"enabled": True, "rbac_role_mappings": [_ADMIN_MAPPING]}},
            )

    def test_region_subsets_are_checked_against_the_regional_deployment_regions(
        self, valid_cdk_context
    ) -> None:
        with pytest.raises(
            ConfigValidationError, match="not regional deployment regions: eu-west-1"
        ):
            _loader(valid_cdk_context, {"kro": {"enabled": True, "regions": ["eu-west-1"]}})
        loader = _loader(valid_cdk_context, {"kro": {"enabled": True, "regions": ["us-west-2"]}})
        assert loader.get_eks_capabilities_config()["kro"]["regions"] == ["us-west-2"]

    def test_gitops_fence_is_enforced_by_the_loader(self, valid_cdk_context) -> None:
        with pytest.raises(ConfigValidationError, match="may only name the tenant namespaces"):
            _loader(
                valid_cdk_context,
                {"argocd": _argocd(gitops=_gitops(destination_namespaces=["default"]))},
            )

    def test_run_scoped_overrides_context_is_deep_merged(self, valid_cdk_context) -> None:
        """``--context eks_capabilities_overrides=<json>`` enables types without editing cdk.json."""
        context = dict(valid_cdk_context)
        context[caps.EKS_CAPABILITIES_CONTEXT_KEY] = {"argocd": {"idc_region": "us-east-2"}}
        context[caps.EKS_CAPABILITIES_OVERRIDES_CONTEXT_KEY] = json.dumps(
            {"kro": {"enabled": True}, "argocd": _argocd(gitops=_gitops())}
        )
        config = ConfigLoader(cdk.App(context=context)).get_eks_capabilities_config()
        assert config["kro"]["enabled"] is True
        assert config["argocd"]["enabled"] is True
        assert config["argocd"]["idc_region"] == "us-east-2"  # cdk.json value survives
        assert config["argocd"]["gitops"]["repo_url"] == _REPO_URL

    def test_overrides_alone_stand_in_for_an_absent_block(self, valid_cdk_context) -> None:
        context = dict(valid_cdk_context)
        context[caps.EKS_CAPABILITIES_OVERRIDES_CONTEXT_KEY] = {"ack": {"enabled": True}}
        config = ConfigLoader(cdk.App(context=context)).get_eks_capabilities_config()
        assert config["ack"]["enabled"] is True

    @pytest.mark.parametrize(
        ("value", "match"),
        [
            ("not json", "must be a JSON object"),
            ("[1, 2]", "must be a JSON object, got list"),
            (
                json.dumps({"kro": {"enabled": "yes"}}),
                r"eks_capabilities\.kro\.enabled must be a boolean",
            ),
            (
                json.dumps({"kro": {"enabled": True, "regions": ["eu-west-1"]}}),
                "not regional deployment regions",
            ),
        ],
    )
    def test_malformed_overrides_fail_at_construction(
        self, valid_cdk_context, value: str, match: str
    ) -> None:
        context = dict(valid_cdk_context)
        context[caps.EKS_CAPABILITIES_OVERRIDES_CONTEXT_KEY] = value
        with pytest.raises(ConfigValidationError, match=match):
            ConfigLoader(cdk.App(context=context))


class TestOverridesHelpers:
    def test_parse_accepts_none_empty_mapping_and_json(self) -> None:
        assert caps.parse_eks_capabilities_overrides(None) == {}
        assert caps.parse_eks_capabilities_overrides("") == {}
        assert caps.parse_eks_capabilities_overrides("  ") == {}
        assert caps.parse_eks_capabilities_overrides({"kro": {"enabled": True}}) == {
            "kro": {"enabled": True}
        }
        assert caps.parse_eks_capabilities_overrides('{"kro": {"enabled": true}}') == {
            "kro": {"enabled": True}
        }

    def test_parse_rejects_other_shapes(self) -> None:
        with pytest.raises(
            caps.EksCapabilitiesConfigError, match="JSON object string or an object"
        ):
            caps.parse_eks_capabilities_overrides(42)

    def test_merge_semantics(self) -> None:
        assert caps.merge_eks_capabilities_overrides(None, {}) is None
        assert caps.merge_eks_capabilities_overrides({"kro": {"enabled": False}}, {}) == {
            "kro": {"enabled": False}
        }
        assert caps.merge_eks_capabilities_overrides(None, {"kro": {"enabled": True}}) == {
            "kro": {"enabled": True}
        }
        merged = caps.merge_eks_capabilities_overrides(
            {"argocd": {"idc_region": "us-east-2", "gitops": {"path": "apps"}}},
            {"argocd": {"enabled": True, "gitops": {"enabled": True}}},
        )
        assert merged == {
            "argocd": {
                "idc_region": "us-east-2",
                "enabled": True,
                "gitops": {"path": "apps", "enabled": True},
            }
        }
        # A malformed block is left for validation to name.
        assert caps.merge_eks_capabilities_overrides("oops", {"kro": {}}) == "oops"


# ─── Regional stack synthesis ────────────────────────────────────────────────

_EVERYTHING_ON: dict[str, Any] = caps.normalize_eks_capabilities_config(
    {
        "argocd": _argocd(
            rbac_role_mappings=[_ADMIN_MAPPING, _VIEWER_MAPPING],
            idc_region="us-east-2",
            vpce_ids=["vpce-0123456789abcdef0"],
            repo_credentials_secret_arns=[_SECRET_ARN, _SECRET_ARN_PATTERN],
            repo_credentials_kms_key_arns=[_KMS_KEY_ARN],
            gitops=_gitops(
                revision="main",
                path="clusters/{region}/{cluster_name}",
                destination_namespaces=["gco-inference", "gco-jobs"],
                sync_policy="automated",
            ),
        ),
        "ack": {
            "enabled": True,
            "disabled_services": ["ec2", "rds"],
            "enable_cross_namespace": True,
            "assume_role_arns": [_ACK_TARGET_ROLE, _ACK_TARGET_PATTERN],
        },
        "kro": {"enabled": True},
    }
)

# Argo CD selected for another region, ACK for this one, kro nowhere: proves
# the per-type ``regions`` subset and that only ACK's resources appear.
_SUBSET: dict[str, Any] = caps.normalize_eks_capabilities_config(
    {
        "argocd": _argocd(regions=[_OTHER_REGION], gitops=_gitops()),
        "ack": {"enabled": True, "regions": [_REGION]},
        "kro": {"enabled": False, "regions": [_REGION]},
    }
)

# The batteries-included shape: `gitops: {enabled: true}` and nothing else,
# so the stack owns the repository (default source, default path, default
# removal policy).
_CODECOMMIT: dict[str, Any] = caps.normalize_eks_capabilities_config(
    {"argocd": _argocd(gitops=_gitops_codecommit())}
)

_CODECOMMIT_RETAINED: dict[str, Any] = caps.normalize_eks_capabilities_config(
    {"argocd": _argocd(gitops=_gitops_codecommit(codecommit={"removal_policy": "retain"}))}
)


def _synth(block: dict[str, Any]) -> tuple[GCORegionalStack, dict[str, Any]]:
    app = cdk.App()
    config = MockConfigLoader(app)
    with (
        patch.object(
            MockConfigLoader, "get_eks_capabilities_config", lambda self: copy.deepcopy(block)
        ),
        patch("gco.stacks.regional_stack.ecr_assets.DockerImageAsset") as mock_docker,
        patch.object(
            GCORegionalStack,
            "_create_helm_installer_lambda",
            _RegionalStackSynthesisFixtures._mock_helm_installer,
        ),
    ):
        mock_image = MagicMock()
        mock_image.image_uri = f"{_ACCOUNT}.dkr.ecr.{_REGION}.amazonaws.com/test:latest"
        mock_docker.return_value = mock_image
        stack = GCORegionalStack(
            app,
            "test-eks-capabilities",
            config=config,
            region=_REGION,
            auth_secret_arn=f"arn:aws:secretsmanager:{_REGION}:{_ACCOUNT}:secret:test-secret",  # nosec B106
            env=cdk.Environment(account=_ACCOUNT, region=_REGION),
        )
    return stack, assertions.Template.from_stack(stack).to_json()


@pytest.fixture(scope="module")
def all_off() -> tuple[GCORegionalStack, dict[str, Any]]:
    return _synth(caps.normalize_eks_capabilities_config(None))


@pytest.fixture(scope="module")
def everything_on() -> tuple[GCORegionalStack, dict[str, Any]]:
    return _synth(_EVERYTHING_ON)


@pytest.fixture(scope="module")
def subset() -> tuple[GCORegionalStack, dict[str, Any]]:
    return _synth(_SUBSET)


@pytest.fixture(scope="module")
def codecommit_on() -> tuple[GCORegionalStack, dict[str, Any]]:
    return _synth(_CODECOMMIT)


@pytest.fixture(scope="module")
def codecommit_retained() -> tuple[GCORegionalStack, dict[str, Any]]:
    return _synth(_CODECOMMIT_RETAINED)


def _resources(template: dict[str, Any], resource_type: str) -> dict[str, dict[str, Any]]:
    return {
        logical_id: resource
        for logical_id, resource in template["Resources"].items()
        if resource["Type"] == resource_type
    }


def _capabilities(template: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return _resources(template, "AWS::EKS::Capability")


def _capability_roles(template: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {
        logical_id: resource
        for logical_id, resource in _resources(template, "AWS::IAM::Role").items()
        if logical_id.startswith("EksCapability")
    }


def _trigger(template: dict[str, Any]) -> dict[str, Any]:
    matches = [
        resource
        for logical_id, resource in template["Resources"].items()
        if logical_id.startswith("HelmInstallCharts")
        and resource["Type"] == "AWS::CloudFormation::CustomResource"
    ]
    assert len(matches) == 1
    return matches[0]


def _replacements(template: dict[str, Any]) -> dict[str, Any]:
    return _trigger(template)["Properties"]["ImageReplacements"]


def _role_logical_id(template: dict[str, Any], suffix: str) -> str:
    matches = [
        lid for lid in _capability_roles(template) if lid.startswith(f"EksCapability{suffix}Role")
    ]
    assert len(matches) == 1, matches
    return matches[0]


class TestShippedDefaultSynthesizesNothing:
    def test_no_capability_or_role_resources(self, all_off) -> None:
        _stack, template = all_off
        assert _capabilities(template) == {}
        assert _capability_roles(template) == {}
        assert not [key for key in template.get("Outputs", {}) if key.startswith("EksCapability")]

    def test_no_argocd_tokens_but_the_cluster_arn_is_always_available(self, all_off) -> None:
        _stack, template = all_off
        replacements = _replacements(template)
        assert not [key for key in replacements if key.startswith("{{ARGOCD_")]
        assert replacements["{{EKS_CLUSTER_ARN}}"] == {
            "Fn::GetAtt": [_cluster_logical_id(template), "Arn"]
        }

    def test_stack_exposes_empty_registries(self, all_off) -> None:
        stack, _template = all_off
        assert stack.eks_capabilities == {}
        assert stack.eks_capability_roles == {}
        assert stack.eks_capabilities_config == caps.EKS_CAPABILITIES_DEFAULTS

    def test_config_doubles_without_the_getter_read_as_off(self) -> None:
        """Stack tests built on older config mocks must keep synthesizing unchanged."""
        stack = GCORegionalStack.__new__(GCORegionalStack)
        stack.config = MagicMock(spec=[])  # no get_eks_capabilities_config at all
        assert stack._eks_capabilities_config() == caps.EKS_CAPABILITIES_DEFAULTS
        stack.config = MagicMock()  # a MagicMock getter returns a MagicMock, not a dict
        assert stack._eks_capabilities_config() == caps.EKS_CAPABILITIES_DEFAULTS


def _cluster_logical_id(template: dict[str, Any]) -> str:
    clusters = list(_resources(template, "AWS::EKS::Cluster"))
    assert len(clusters) == 1
    return clusters[0]


class TestEverythingOn:
    def test_one_capability_per_type_with_deterministic_names(self, everything_on) -> None:
        _stack, template = everything_on
        capabilities = _capabilities(template)
        assert set(capabilities) == {"EksCapabilityArgoCd", "EksCapabilityAck", "EksCapabilityKro"}
        cluster = _cluster_logical_id(template)
        expected_types = {
            "EksCapabilityArgoCd": "ARGOCD",
            "EksCapabilityAck": "ACK",
            "EksCapabilityKro": "KRO",
        }
        expected_names = {
            "EksCapabilityArgoCd": "gco-test-argocd",
            "EksCapabilityAck": "gco-test-ack",
            "EksCapabilityKro": "gco-test-kro",
        }
        for logical_id, resource in capabilities.items():
            properties = resource["Properties"]
            assert properties["Type"] == expected_types[logical_id]
            assert properties["CapabilityName"] == expected_names[logical_id]
            assert properties["ClusterName"] == {"Ref": cluster}
            assert properties["DeletePropagationPolicy"] == "RETAIN"
            role_id = properties["RoleArn"]["Fn::GetAtt"][0]
            assert role_id.startswith(f"{logical_id}Role")
            assert properties["RoleArn"]["Fn::GetAtt"][1] == "Arn"
            # Created after the cluster and its own role; deleted before them.
            assert cluster in resource["DependsOn"]
            assert role_id in resource["DependsOn"]

    def test_argocd_configuration(self, everything_on) -> None:
        _stack, template = everything_on
        argo = _capabilities(template)["EksCapabilityArgoCd"]["Properties"]["Configuration"]
        assert set(argo) == {"ArgoCd"}
        assert argo["ArgoCd"] == {
            "AwsIdc": {"IdcInstanceArn": _IDC_ARN, "IdcRegion": "us-east-2"},
            "Namespace": "argocd",
            "NetworkAccess": {"VpceIds": ["vpce-0123456789abcdef0"]},
            "RbacRoleMappings": [
                {"Role": "ADMIN", "Identities": [{"Id": "u-admin", "Type": "SSO_USER"}]},
                {"Role": "VIEWER", "Identities": [{"Id": "g-viewers", "Type": "SSO_GROUP"}]},
            ],
        }

    def test_ack_configuration_and_kro_has_none(self, everything_on) -> None:
        _stack, template = everything_on
        capabilities = _capabilities(template)
        assert capabilities["EksCapabilityAck"]["Properties"]["Configuration"] == {
            "Ack": {"DisabledServices": ["ec2", "rds"], "EnableCrossNamespace": True}
        }
        assert "Configuration" not in capabilities["EksCapabilityKro"]["Properties"]

    def test_every_capability_role_is_trusted_by_the_capabilities_service_with_session_tags(
        self, everything_on
    ) -> None:
        _stack, template = everything_on
        roles = _capability_roles(template)
        assert len(roles) == 3
        for logical_id, role in roles.items():
            statements = role["Properties"]["AssumeRolePolicyDocument"]["Statement"]
            assert statements == [
                {
                    "Action": ["sts:AssumeRole", "sts:TagSession"],
                    "Effect": "Allow",
                    "Principal": {"Service": "capabilities.eks.amazonaws.com"},
                }
            ], logical_id
            assert "RoleName" not in role["Properties"]
            assert "ManagedPolicyArns" not in role["Properties"]

    def test_argocd_role_reads_exactly_the_configured_secrets(self, everything_on) -> None:
        _stack, template = everything_on
        role = _capability_roles(template)[_role_logical_id(template, "ArgoCd")]
        policies = role["Properties"]["Policies"]
        assert len(policies) == 1
        assert policies[0]["PolicyName"] == "EksCapabilityArgoCdGrants"
        assert policies[0]["PolicyDocument"]["Statement"] == [
            {
                "Action": ["secretsmanager:GetSecretValue", "secretsmanager:DescribeSecret"],
                "Effect": "Allow",
                "Resource": [_SECRET_ARN, _SECRET_ARN_PATTERN],
                "Sid": "ReadGitRepositoryCredentials",
            },
            {
                "Action": "kms:Decrypt",
                "Condition": {
                    "StringEquals": {
                        "kms:ViaService": {
                            "Fn::Join": [
                                "",
                                [f"secretsmanager.{_REGION}.", {"Ref": "AWS::URLSuffix"}],
                            ]
                        }
                    }
                },
                "Effect": "Allow",
                "Resource": _KMS_KEY_ARN,
                "Sid": "DecryptGitRepositoryCredentials",
            },
        ]

    def test_ack_role_assumes_exactly_the_configured_roles(self, everything_on) -> None:
        _stack, template = everything_on
        role = _capability_roles(template)[_role_logical_id(template, "Ack")]
        assert role["Properties"]["Policies"][0]["PolicyDocument"]["Statement"] == [
            {
                "Action": "sts:AssumeRole",
                "Effect": "Allow",
                "Resource": [_ACK_TARGET_ROLE, _ACK_TARGET_PATTERN],
                "Sid": "AssumeAckControllerRoles",
            }
        ]

    def test_kro_role_has_no_aws_permissions(self, everything_on) -> None:
        _stack, template = everything_on
        role = _capability_roles(template)[_role_logical_id(template, "Kro")]
        assert "Policies" not in role["Properties"]
        assert not _resources(template, "AWS::IAM::Policy") or all(
            "EksCapability" not in lid for lid in _resources(template, "AWS::IAM::Policy")
        )

    def test_only_operator_configured_wildcards_are_acknowledged_for_cdk_nag(
        self, everything_on
    ) -> None:
        stack, _template = everything_on
        key = cdk.Validations.ACKNOWLEDGED_RULES_METADATA_KEY

        def acknowledged(role_id: str) -> dict[str, str]:
            role = stack.node.find_child(role_id)
            entries = [entry.data for entry in role.node.metadata if entry.type == key]
            merged: dict[str, str] = {}
            for entry in entries:
                merged.update(entry)
            return merged

        argo = acknowledged("EksCapabilityArgoCdRole")
        assert set(argo) == {f"AwsSolutions-IAM5[Resource::{_SECRET_ARN_PATTERN}]"}
        ack = acknowledged("EksCapabilityAckRole")
        assert set(ack) == {f"AwsSolutions-IAM5[Resource::{_ACK_TARGET_PATTERN}]"}
        assert "Resource::*" not in json.dumps([argo, ack])
        assert acknowledged("EksCapabilityKroRole") == {}

    def test_outputs(self, everything_on) -> None:
        _stack, template = everything_on
        outputs = {
            key: value
            for key, value in template["Outputs"].items()
            if key.startswith("EksCapability")
        }
        assert set(outputs) == {
            "EksCapabilityArgoCdArn",
            "EksCapabilityArgoCdRoleArn",
            "EksCapabilityArgoCdServerUrl",
            "EksCapabilityAckArn",
            "EksCapabilityAckRoleArn",
            "EksCapabilityKroArn",
            "EksCapabilityKroRoleArn",
        }
        assert outputs["EksCapabilityArgoCdArn"]["Value"] == {
            "Fn::GetAtt": ["EksCapabilityArgoCd", "Arn"]
        }
        assert outputs["EksCapabilityArgoCdServerUrl"]["Value"] == {
            "Fn::GetAtt": ["EksCapabilityArgoCd", "Configuration.ArgoCd.ServerUrl"]
        }
        assert outputs["EksCapabilityKroRoleArn"]["Value"] == {
            "Fn::GetAtt": [_role_logical_id(template, "Kro"), "Arn"]
        }

    def test_convergence_trigger_waits_for_every_capability(self, everything_on) -> None:
        """The applier renders argoproj.io objects; their CRDs come from the ACTIVE capability."""
        _stack, template = everything_on
        depends_on = set(_trigger(template)["DependsOn"])
        assert {"EksCapabilityArgoCd", "EksCapabilityAck", "EksCapabilityKro"} <= depends_on

    def test_stack_registries(self, everything_on) -> None:
        stack, _template = everything_on
        assert list(stack.eks_capabilities) == ["argocd", "ack", "kro"]
        assert list(stack.eks_capability_roles) == ["argocd", "ack", "kro"]
        assert stack.eks_capabilities_config == _EVERYTHING_ON

    def test_applier_tokens_gate_both_argocd_manifests(self, everything_on) -> None:
        _stack, template = everything_on
        replacements = _replacements(template)
        cluster = _cluster_logical_id(template)
        argo_role = _role_logical_id(template, "ArgoCd")
        assert replacements["{{ARGOCD_CAPABILITY_ROLE_ARN}}"] == {"Fn::GetAtt": [argo_role, "Arn"]}
        assert replacements["{{ARGOCD_GITOPS_REPO_URL}}"] == _REPO_URL
        assert replacements["{{ARGOCD_GITOPS_REVISION}}"] == "main"
        assert replacements["{{ARGOCD_GITOPS_DEFAULT_NAMESPACE}}"] == "gco-inference"
        assert replacements["{{ARGOCD_GITOPS_SYNC_POLICY}}"] == json.dumps(
            {"automated": {"selfHeal": True, "prune": False}}
        )
        # {cluster_name} resolves through the configured (literal) cluster name,
        # not the cluster resource's token: the same name also names the
        # CodeCommit repository, so nothing here is a deploy-time join.
        assert replacements["{{ARGOCD_GITOPS_PATH}}"] == "clusters/us-east-1/gco-test-us-east-1"
        # source: git leaves the operator's repository URL untouched and creates
        # no repository of its own.
        assert _resources(template, "AWS::CodeCommit::Repository") == {}
        # Destinations carry the cluster ARN (the hosted capability identifies
        # clusters by ARN) once per configured namespace, in config order.
        destinations = replacements["{{ARGOCD_GITOPS_DESTINATIONS}}"]["Fn::Join"][1]
        rendered = "".join(part if isinstance(part, str) else "<ARN>" for part in destinations)
        assert json.loads(rendered) == [
            {"server": "<ARN>", "namespace": "gco-inference"},
            {"server": "<ARN>", "namespace": "gco-jobs"},
        ]
        assert destinations.count({"Fn::GetAtt": [cluster, "Arn"]}) == 2
        assert set(caps.EKS_CAPABILITIES_MANIFEST_TOKENS) <= set(replacements)


class TestPerRegionSubset:
    def test_only_the_types_selected_for_this_region_synthesize(self, subset) -> None:
        stack, template = subset
        assert set(_capabilities(template)) == {"EksCapabilityAck"}
        assert set(_capability_roles(template)) == {_role_logical_id(template, "Ack")}
        assert list(stack.eks_capabilities) == ["ack"]
        assert set(_trigger(template)["DependsOn"]) >= {"EksCapabilityAck"}
        assert "EksCapabilityArgoCd" not in template["Resources"]
        assert "EksCapabilityKro" not in template["Resources"]

    def test_ack_without_grants_has_no_policy(self, subset) -> None:
        _stack, template = subset
        role = _capability_roles(template)[_role_logical_id(template, "Ack")]
        assert "Policies" not in role["Properties"]
        assert _capabilities(template)["EksCapabilityAck"]["Properties"]["Configuration"] == {
            "Ack": {"EnableCrossNamespace": False}
        }

    def test_argocd_selected_elsewhere_emits_no_argocd_tokens_here(self, subset) -> None:
        _stack, template = subset
        replacements = _replacements(template)
        assert not [key for key in replacements if key.startswith("{{ARGOCD_")]
        outputs = {key for key in template["Outputs"] if key.startswith("EksCapability")}
        assert outputs == {"EksCapabilityAckArn", "EksCapabilityAckRoleArn"}
        assert _resources(template, "AWS::CodeCommit::Repository") == {}


def _gitops_repository(template: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    repositories = _resources(template, "AWS::CodeCommit::Repository")
    assert len(repositories) == 1, list(repositories)
    logical_id, repository = next(iter(repositories.items()))
    # L2 construct: the logical id carries CDK's hash suffix.
    assert logical_id.startswith("EksCapabilityArgoCdGitOpsRepository"), logical_id
    return logical_id, repository


class TestCodeCommitSource:
    """``gitops: {enabled: true}`` alone gives the cluster a GCO-managed repository."""

    def test_one_repository_named_after_the_cluster_and_seeded(self, codecommit_on) -> None:
        stack, template = codecommit_on
        _logical_id, repository = _gitops_repository(template)
        properties = repository["Properties"]
        assert properties["RepositoryName"] == "gco-test-us-east-1-gitops"
        assert "gco stacks capabilities gitops push" in properties["RepositoryDescription"]
        # Seeded from the checked-in README so the root Application is
        # Synced/Healthy before the first push; CloudFormation applies the
        # seed at creation only.
        assert properties["Code"]["BranchName"] == "main"
        assert set(properties["Code"]["S3"]) >= {"Bucket", "Key"}
        assert stack.gitops_repository is not None
        assert GCORegionalStack._GITOPS_CODECOMMIT_SEED_DIR.is_dir()
        seed_files = sorted(
            path.name for path in GCORegionalStack._GITOPS_CODECOMMIT_SEED_DIR.iterdir()
        )
        assert seed_files == ["README.md"], "the seed must stay a README-only tree"

    def test_destroy_is_the_default_removal_policy(self, codecommit_on) -> None:
        _stack, template = codecommit_on
        _logical_id, repository = _gitops_repository(template)
        assert repository["DeletionPolicy"] == "Delete"
        assert repository["UpdateReplacePolicy"] == "Delete"

    def test_retain_keeps_the_history(self, codecommit_retained) -> None:
        _stack, template = codecommit_retained
        _logical_id, repository = _gitops_repository(template)
        assert repository["DeletionPolicy"] == "Retain"
        assert repository["UpdateReplacePolicy"] == "Retain"

    def test_argocd_role_may_only_git_pull_that_repository(self, codecommit_on) -> None:
        _stack, template = codecommit_on
        logical_id, _repository = _gitops_repository(template)
        role = _capability_roles(template)[_role_logical_id(template, "ArgoCd")]
        assert role["Properties"]["Policies"][0]["PolicyDocument"]["Statement"] == [
            {
                "Action": "codecommit:GitPull",
                "Effect": "Allow",
                "Resource": {"Fn::GetAtt": [logical_id, "Arn"]},
                "Sid": "PullGitOpsRepository",
            }
        ]

    def test_repository_outputs(self, codecommit_on) -> None:
        _stack, template = codecommit_on
        logical_id, _repository = _gitops_repository(template)
        outputs = template["Outputs"]
        assert outputs["EksCapabilityArgoCdGitOpsRepositoryName"]["Value"] == {
            "Fn::GetAtt": [logical_id, "Name"]
        }
        assert outputs["EksCapabilityArgoCdGitOpsRepositoryCloneUrlHttp"]["Value"] == {
            "Fn::GetAtt": [logical_id, "CloneUrlHttp"]
        }

    def test_root_application_reads_the_repository_root(self, codecommit_on) -> None:
        _stack, template = codecommit_on
        replacements = _replacements(template)
        # The URL is derived from the deterministic repository name (the same
        # form the CLI and the live harness compute), with the partition
        # suffix left to CloudFormation.
        assert replacements["{{ARGOCD_GITOPS_REPO_URL}}"] == {
            "Fn::Join": [
                "",
                [
                    "https://git-codecommit.us-east-1.",
                    {"Ref": "AWS::URLSuffix"},
                    "/v1/repos/gco-test-us-east-1-gitops",
                ],
            ]
        }
        assert replacements["{{ARGOCD_GITOPS_PATH}}"] == "."
        assert replacements["{{ARGOCD_GITOPS_REVISION}}"] == "HEAD"
        assert replacements["{{ARGOCD_GITOPS_SYNC_POLICY}}"] == "{}"


# ─── The pure replacements helper ────────────────────────────────────────────


class TestComputeReplacements:
    _CLUSTER_ARN = f"arn:aws:eks:{_REGION}:{_ACCOUNT}:cluster/gco-us-east-1"
    _ROLE_ARN = f"arn:aws:iam::{_ACCOUNT}:role/argocd-capability"

    def _compute(self, block: dict[str, Any], role_arn: str | None = _ROLE_ARN) -> dict[str, str]:
        return caps.compute_eks_capabilities_replacements(
            caps.normalize_eks_capabilities_config(block),
            region=_REGION,
            cluster_name="gco-us-east-1",
            cluster_arn=self._CLUSTER_ARN,
            argocd_role_arn=role_arn,
        )

    def test_no_role_means_no_tokens_at_all(self) -> None:
        assert self._compute({"argocd": _argocd(gitops=_gitops())}, role_arn=None) == {}

    def test_capability_without_gitops_emits_only_the_role_gate(self) -> None:
        assert self._compute({"argocd": _argocd()}) == {
            "{{ARGOCD_CAPABILITY_ROLE_ARN}}": self._ROLE_ARN
        }

    def test_manual_sync_policy_is_an_empty_object(self) -> None:
        replacements = self._compute({"argocd": _argocd(gitops=_gitops())})
        assert set(replacements) == set(caps.EKS_CAPABILITIES_MANIFEST_TOKENS)
        assert replacements["{{ARGOCD_GITOPS_SYNC_POLICY}}"] == "{}"
        assert replacements["{{ARGOCD_GITOPS_REVISION}}"] == "HEAD"
        assert replacements["{{ARGOCD_GITOPS_PATH}}"] == "clusters/us-east-1"
        assert replacements["{{ARGOCD_GITOPS_DEFAULT_NAMESPACE}}"] == "gco-jobs"
        assert json.loads(replacements["{{ARGOCD_GITOPS_DESTINATIONS}}"]) == [
            {"server": self._CLUSTER_ARN, "namespace": "gco-jobs"},
            {"server": self._CLUSTER_ARN, "namespace": "gco-inference"},
        ]

    def test_automated_sync_self_heals_but_never_prunes(self) -> None:
        replacements = self._compute({"argocd": _argocd(gitops=_gitops(sync_policy="automated"))})
        assert json.loads(replacements["{{ARGOCD_GITOPS_SYNC_POLICY}}"]) == {
            "automated": {"selfHeal": True, "prune": False}
        }

    def test_values_are_stripped_and_path_placeholders_rendered(self) -> None:
        replacements = self._compute(
            {
                "argocd": _argocd(
                    gitops=_gitops(
                        repo_url=f" {_REPO_URL} ", revision=" v1 ", path=" apps/{cluster_name} "
                    )
                )
            }
        )
        assert replacements["{{ARGOCD_GITOPS_REPO_URL}}"] == _REPO_URL
        assert replacements["{{ARGOCD_GITOPS_REVISION}}"] == "v1"
        assert replacements["{{ARGOCD_GITOPS_PATH}}"] == "apps/gco-us-east-1"

    def test_codecommit_source_derives_the_repository_url_and_reads_the_root(self) -> None:
        replacements = self._compute({"argocd": _argocd(gitops=_gitops_codecommit())})
        assert set(replacements) == set(caps.EKS_CAPABILITIES_MANIFEST_TOKENS)
        assert (
            replacements["{{ARGOCD_GITOPS_REPO_URL}}"]
            == "https://git-codecommit.us-east-1.amazonaws.com/v1/repos/gco-us-east-1-gitops"
        )
        assert replacements["{{ARGOCD_GITOPS_PATH}}"] == "."
        # An explicit path still wins, placeholders included.
        custom = self._compute(
            {"argocd": _argocd(gitops=_gitops_codecommit(path="overlays/{cluster_name}"))}
        )
        assert custom["{{ARGOCD_GITOPS_PATH}}"] == "overlays/gco-us-east-1"
        # The partition suffix is a parameter so the stack can hand in a token.
        china = caps.compute_eks_capabilities_replacements(
            caps.normalize_eks_capabilities_config(
                {"argocd": _argocd(gitops=_gitops_codecommit())}
            ),
            region="cn-north-1",
            cluster_name="gco-cn-north-1",
            cluster_arn=self._CLUSTER_ARN,
            argocd_role_arn=self._ROLE_ARN,
            url_suffix=caps.aws_url_suffix_for_region("cn-north-1"),
        )
        assert (
            china["{{ARGOCD_GITOPS_REPO_URL}}"]
            == "https://git-codecommit.cn-north-1.amazonaws.com.cn/v1/repos/gco-cn-north-1-gitops"
        )

    def test_rendered_json_never_forms_an_unresolved_placeholder(self) -> None:
        """The applier gates on ``{{UPPER_SNAKE}}``; JSON braces must not look like one."""
        replacements = self._compute({"argocd": _argocd(gitops=_gitops(sync_policy="automated"))})
        for value in replacements.values():
            assert not re.search(r"\{\{[A-Z0-9_]+\}\}", value)
            assert "{{" not in value

    def test_kind_ci_applies_and_prunes_both_manifests_with_the_shared_renderer(self) -> None:
        """integration:kind:cluster-e2e exercises the real CRDs, the RBAC fence and the prune path."""
        workflow = (
            _MANIFESTS_DIR.parents[2] / ".github/workflows/integration-tests.yml"
        ).read_text(encoding="utf-8")
        e2e = workflow.split('name: "integration:kind:cluster-e2e"', 1)[1].split(
            'name: "integration:kind:cost-pipeline"', 1
        )[0]
        assert "07-argocd-cluster-access.yaml" in e2e and "08-argocd-gitops.yaml" in e2e
        # The same pure renderer the stack uses, not a hand-copied substitution,
        # fed a *validated* block that names the operator-owned source: with the
        # CodeCommit default the renderer would point the Application at a
        # repository that exists only after a regional stack deploys, and the
        # job's sourceRepos assertion below pins the GitHub URL.
        assert "compute_eks_capabilities_replacements(" in e2e
        assert "validate_eks_capabilities_config(" in e2e
        assert '"source": "git",' in e2e
        assert (
            "test \"$(project '{.spec.sourceRepos[0]}')\" = \\\n" + f'            "{_CI_REPO}.git"'
        ) in e2e
        # CRDs come from a pinned upstream release (ARGOCD_VERSION, a job-level
        # env pin the monthly dependency scan tracks and the supply-chain tests
        # bind to a committed checksum per manifest) and must be Established
        # before the manifests are applied.
        assert re.search(r'ARGOCD_VERSION: "v\d+\.\d+\.\d+"', e2e)
        assert "argoproj/argo-cd/${ARGOCD_VERSION}/manifests/crds/application-crd.yaml" in e2e
        assert "argoproj/argo-cd/${ARGOCD_VERSION}/manifests/crds/appproject-crd.yaml" in e2e
        assert "crd/applications.argoproj.io crd/appprojects.argoproj.io" in e2e
        # The fence is proved by impersonating the access-entry group.
        assert '--as-group="${group}"' in e2e
        assert 'can-i create deployments -n gco-system "${as[@]}")" = "no"' in e2e
        # ...and the allow-list inside the tenant namespaces refuses the
        # guardrail kinds the AppProject blacklists (both fences, not one).
        for probe in (
            "create networkpolicies",
            "update resourcequotas",
            "delete limitranges",
            "create rolebindings",
            "create roles",
        ):
            assert f'can-i {probe} -n "${{ns}}" "${{as[@]}}")" = "no"' in e2e
        # The disable path runs the applier's own inventories, which must
        # remove everything but the argocd Namespace.
        for gate in ("{{ARGOCD_GITOPS_REPO_URL}}", "{{ARGOCD_CAPABILITY_ROLE_ARN}}"):
            assert gate in e2e
        assert "handler._FEATURE_RESOURCE_INVENTORY[(placeholder, False)]" in e2e
        assert "handler._delete_exact_resources(inventory" in e2e
        assert "kubectl get namespace argocd" in e2e

    def test_manifest_tokens_are_exactly_the_ones_the_manifests_carry(self) -> None:
        """The stack's token list and the two manifests describe one contract."""
        carried: set[str] = set()
        for name in ("07-argocd-cluster-access.yaml", "08-argocd-gitops.yaml"):
            carried |= set(
                re.findall(
                    r"\{\{[A-Z0-9_]+\}\}", (_MANIFESTS_DIR / name).read_text(encoding="utf-8")
                )
            )
        assert carried == set(caps.EKS_CAPABILITIES_MANIFEST_TOKENS) | {"{{EKS_CLUSTER_ARN}}"}
        assert all(token.startswith("{{ARGOCD_") for token in caps.EKS_CAPABILITIES_MANIFEST_TOKENS)
