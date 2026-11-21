"""Synthesis checks for the deploy-time EKS access surface (W3).

Two knobs on the ``eks_cluster`` block:

* ``developer_access`` — a list of ``{principal_arn, scope, namespaces}``
  entries, each synthesizing one ``AWS::EKS::AccessEntry`` for a human
  principal. The default grant is deliberately narrow (AmazonEKSEditPolicy
  scoped to ``gco-jobs``); ``scope: cluster`` opts into
  AmazonEKSClusterAdminPolicy explicitly. Absent config synthesizes exactly
  the platform-Lambda entries the stack always had.
* ``public_access_cidrs`` — the allowlist for a PUBLIC_AND_PRIVATE endpoint.
  Enabling public access with no allowlist is a loud synth-time warning
  naming the 0.0.0.0/0 exposure, never a silent default.

Config errors fail synthesis: a typo'd access grant must never silently
synthesize as nothing.

``MockConfigLoader.get_eks_cluster_config`` returns a fixed PRIVATE config,
so each case patches that method rather than app context. The helm-installer
fixture mocks that Lambda away, so the platform baseline here is the
kubectl-applier and GA-registration entries (two); the real stack adds the
helm installer's for three.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock, patch

import aws_cdk as cdk
import pytest
from aws_cdk import assertions

from gco.stacks.regional_stack import GCORegionalStack
from tests.test_regional_stack import MockConfigLoader
from tests.test_regional_stack import TestRegionalStackSynthesis as _RegionalStackSynthesisFixtures

_ACCOUNT = "123456789012"
_REGION = "us-east-1"
_DEV_ROLE = f"arn:aws:iam::{_ACCOUNT}:role/DeveloperRole"
_DEV_USER = f"arn:aws:iam::{_ACCOUNT}:user/alice"

_EKS_DEFAULTS: dict[str, Any] = {
    "endpoint_access": "PRIVATE",
    "public_access_cidrs": [],
    "developer_access": [],
}

# The access entries the mocked platform baseline synthesizes: two platform
# entries plus the MCP server role's read-only entry (the real stack also adds
# the helm installer's, mocked away by the fixture).
_PLATFORM_ENTRY_COUNT = 3


def _synth_stack(eks_cluster: dict[str, Any] | None = None) -> GCORegionalStack:
    """Synthesize the regional stack with an eks_cluster config override."""
    merged = {**_EKS_DEFAULTS, **(eks_cluster or {})}
    app = cdk.App()
    config = MockConfigLoader(app)

    with (
        patch.object(MockConfigLoader, "get_eks_cluster_config", lambda self: dict(merged)),
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

        return GCORegionalStack(
            app,
            "test-eks-access-config",
            config=config,
            region=_REGION,
            auth_secret_arn=f"arn:aws:secretsmanager:{_REGION}:{_ACCOUNT}:secret:test-secret",  # nosec B106
            env=cdk.Environment(account=_ACCOUNT, region=_REGION),
        )


def _template(eks_cluster: dict[str, Any] | None = None) -> dict[str, Any]:
    return assertions.Template.from_stack(_synth_stack(eks_cluster)).to_json()


def _warning_messages(stack: GCORegionalStack) -> list[str]:
    findings = assertions.Annotations.from_stack(stack).find_warning(
        "*", assertions.Match.any_value()
    )
    return [str(finding.entry.data) for finding in findings]


def _access_entries(template: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        resource["Properties"]
        for resource in template.get("Resources", {}).values()
        if resource.get("Type") == "AWS::EKS::AccessEntry"
    ]


def _entry_for(template: dict[str, Any], principal: str) -> dict[str, Any]:
    matches = [e for e in _access_entries(template) if e.get("PrincipalArn") == principal]
    assert len(matches) == 1, f"expected one access entry for {principal}, got {len(matches)}"
    return matches[0]


def _policy_arn_suffix(policy: dict[str, Any]) -> str:
    return policy["PolicyArn"]["Fn::Join"][1][-1]


class TestDeveloperAccessEntries:
    def test_absent_config_synthesizes_only_the_platform_entries(self) -> None:
        """Purely additive: no developer_access means exactly today's entries."""
        assert len(_access_entries(_template())) == _PLATFORM_ENTRY_COUNT

    def test_two_entries_default_to_namespace_scope_on_gco_jobs(self) -> None:
        template = _template(
            {
                "developer_access": [
                    {"principal_arn": _DEV_ROLE},
                    {"principal_arn": _DEV_USER},
                ]
            }
        )
        assert len(_access_entries(template)) == _PLATFORM_ENTRY_COUNT + 2

        for principal in (_DEV_ROLE, _DEV_USER):
            entry = _entry_for(template, principal)
            policies = entry["AccessPolicies"]
            assert len(policies) == 1
            assert policies[0]["AccessScope"] == {
                "Type": "namespace",
                "Namespaces": ["gco-jobs"],
            }
            assert _policy_arn_suffix(policies[0]).endswith(
                "cluster-access-policy/AmazonEKSEditPolicy"
            )

    def test_explicit_namespaces_are_respected(self) -> None:
        template = _template(
            {
                "developer_access": [
                    {
                        "principal_arn": _DEV_ROLE,
                        "scope": "namespace",
                        "namespaces": ["gco-jobs", "gco-inference"],
                    }
                ]
            }
        )
        entry = _entry_for(template, _DEV_ROLE)
        assert entry["AccessPolicies"][0]["AccessScope"] == {
            "Type": "namespace",
            "Namespaces": ["gco-jobs", "gco-inference"],
        }

    def test_cluster_scope_is_an_explicit_opt_in_to_cluster_admin(self) -> None:
        template = _template(
            {"developer_access": [{"principal_arn": _DEV_ROLE, "scope": "cluster"}]}
        )
        entry = _entry_for(template, _DEV_ROLE)
        policy = entry["AccessPolicies"][0]
        assert policy["AccessScope"] == {"Type": "cluster"}
        assert _policy_arn_suffix(policy).endswith(
            "cluster-access-policy/AmazonEKSClusterAdminPolicy"
        )

    @pytest.mark.parametrize(
        ("developer_access", "match"),
        [
            ("not-a-list", "must be a list"),
            (["not-a-dict"], "must be an object"),
            ([{"principal_arn": "DeveloperRole"}], "must be an IAM principal ARN"),
            ([{}], "must be an IAM principal ARN"),
            ([{"principal_arn": _DEV_ROLE, "scope": "global"}], "scope must be 'namespace'"),
            (
                [{"principal_arn": _DEV_ROLE, "namespaces": "gco-jobs"}],
                "namespaces must be a list",
            ),
            (
                [{"principal_arn": _DEV_ROLE, "namespaces": [""]}],
                "namespaces must be a list",
            ),
        ],
    )
    def test_config_errors_fail_synthesis(self, developer_access: Any, match: str) -> None:
        with pytest.raises(ValueError, match=match):
            _template({"developer_access": developer_access})


class TestPublicAccessCidrs:
    @staticmethod
    def _vpc_config(template: dict[str, Any]) -> dict[str, Any]:
        clusters = [
            resource
            for resource in template.get("Resources", {}).values()
            if resource.get("Type") == "AWS::EKS::Cluster"
        ]
        assert len(clusters) == 1
        return clusters[0]["Properties"]["ResourcesVpcConfig"]

    def test_public_with_allowlist_restricts_the_endpoint(self) -> None:
        template = _template(
            {
                "endpoint_access": "PUBLIC_AND_PRIVATE",
                "public_access_cidrs": ["203.0.113.7/32", "198.51.100.0/24"],
            }
        )
        vpc_config = self._vpc_config(template)
        assert vpc_config["EndpointPublicAccess"] is True
        assert vpc_config["PublicAccessCidrs"] == ["203.0.113.7/32", "198.51.100.0/24"]

    def test_public_without_allowlist_warns_about_the_open_endpoint(self) -> None:
        stack = _synth_stack({"endpoint_access": "PUBLIC_AND_PRIVATE"})
        open_endpoint_warnings = [w for w in _warning_messages(stack) if "0.0.0.0/0" in w]
        assert open_endpoint_warnings, "expected a 0.0.0.0/0 exposure warning"
        assert any("public_access_cidrs" in w for w in open_endpoint_warnings)

    def test_public_with_allowlist_does_not_warn(self) -> None:
        stack = _synth_stack(
            {
                "endpoint_access": "PUBLIC_AND_PRIVATE",
                "public_access_cidrs": ["203.0.113.7/32"],
            }
        )
        assert not [w for w in _warning_messages(stack) if "0.0.0.0/0" in w]

    def test_private_default_does_not_warn(self) -> None:
        assert not [w for w in _warning_messages(_synth_stack()) if "0.0.0.0/0" in w]

    def test_private_default_keeps_the_public_endpoint_off(self) -> None:
        vpc_config = self._vpc_config(_template())
        assert vpc_config["EndpointPublicAccess"] is False
        assert vpc_config["EndpointPrivateAccess"] is True
