"""EKS Capabilities (AWS-managed ACK / kro).

Three layers, one contract:

* ``gco/eks_capabilities_config.py`` — the ``eks_capabilities`` cdk.json
  block: every type off by default, per-type ``regions`` subsets, ACK's
  permission inputs (assumable roles and managed policies), the kro user name
  the tenant RBAC binds, and the loader wrapping its errors as
  ``ConfigValidationError``.
* ``gco/stacks/regional_stack.py`` — one capability IAM role (trusted by
  ``capabilities.eks.amazonaws.com`` with ``sts:AssumeRole`` +
  ``sts:TagSession``) and one ``AWS::EKS::Capability`` per enabled type, the
  outputs, and the kubectl-applier token that gates
  ``07-kro-tenant-access.yaml``.
* The shipped default synthesizes exactly today's template: no capability
  resources and no ``{{KRO_*}}`` token.

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
import yaml
from aws_cdk import assertions

from gco import eks_capabilities_config as caps
from gco.config.config_loader import ConfigLoader, ConfigValidationError
from gco.stacks.regional_stack import GCORegionalStack
from tests.test_regional_stack import MockConfigLoader
from tests.test_regional_stack import TestRegionalStackSynthesis as _RegionalStackSynthesisFixtures

_ACCOUNT = "123456789012"
_REGION = "us-east-1"
_OTHER_REGION = "us-west-2"
_ACK_TARGET_ROLE = f"arn:aws:iam::{_ACCOUNT}:role/ack-s3-controller"
_ACK_TARGET_PATTERN = f"arn:aws:iam::{_ACCOUNT}:role/ack-*"
_ACK_AWS_POLICY = "arn:aws:iam::aws:policy/AmazonSQSFullAccess"
_ACK_CUSTOMER_POLICY = f"arn:aws:iam::{_ACCOUNT}:policy/gco/ack-s3-buckets"
_MANIFESTS_DIR = Path(__file__).resolve().parent.parent / "lambda/kubectl-applier-simple/manifests"
_KRO_MANIFEST = _MANIFESTS_DIR / "07-kro-tenant-access.yaml"


# ─── gco/eks_capabilities_config.py ──────────────────────────────────────────


class TestDefaultsAndNormalization:
    def test_every_type_is_off_by_default(self) -> None:
        for type_name in caps.EKS_CAPABILITY_TYPES:
            block = caps.EKS_CAPABILITIES_DEFAULTS[type_name]
            assert block["enabled"] is False
            assert block["regions"] == []
        # ACK can call no AWS API until the operator grants it something.
        assert caps.EKS_CAPABILITIES_DEFAULTS["ack"]["assume_role_arns"] == []
        assert caps.EKS_CAPABILITIES_DEFAULTS["ack"]["iam_policy_arns"] == []

    def test_argo_cd_is_not_a_capability_type(self) -> None:
        """Argo CD runs in the cluster (helm.argocd); the capability block never offers it."""
        assert caps.EKS_CAPABILITY_TYPES == ("ack", "kro")
        assert "argocd" not in caps.EKS_CAPABILITIES_DEFAULTS

    def test_type_names_map_to_the_api_enum(self) -> None:
        assert caps.CAPABILITY_TYPE_API_NAMES == {"ack": "ACK", "kro": "KRO"}
        assert set(caps.CAPABILITY_TYPE_API_NAMES) == set(caps.EKS_CAPABILITY_TYPES)

    @pytest.mark.parametrize("raw", [None, {}])
    def test_absent_or_empty_block_normalizes_to_the_defaults(self, raw: object) -> None:
        normalized = caps.normalize_eks_capabilities_config(raw)
        assert normalized == caps.EKS_CAPABILITIES_DEFAULTS
        assert normalized is not caps.EKS_CAPABILITIES_DEFAULTS  # a copy, never the constant

    def test_normalize_deep_merges_nested_blocks(self) -> None:
        normalized = caps.normalize_eks_capabilities_config(
            {"ack": {"enabled": True, "iam_policy_arns": [_ACK_AWS_POLICY]}}
        )
        assert normalized["ack"]["enabled"] is True
        assert normalized["ack"]["iam_policy_arns"] == [_ACK_AWS_POLICY]
        assert normalized["ack"]["disabled_services"] == []
        assert normalized["kro"] == caps.EKS_CAPABILITIES_DEFAULTS["kro"]

    def test_normalize_does_not_mutate_the_defaults(self) -> None:
        before = copy.deepcopy(caps.EKS_CAPABILITIES_DEFAULTS)
        normalized = caps.normalize_eks_capabilities_config({"kro": {"enabled": True}})
        normalized["kro"]["regions"].append("eu-west-1")
        assert before == caps.EKS_CAPABILITIES_DEFAULTS

    def test_non_object_block_is_rejected(self) -> None:
        with pytest.raises(caps.EksCapabilitiesConfigError, match="must be an object"):
            caps.normalize_eks_capabilities_config(["kro"])

    def test_validate_none_returns_the_defaults(self) -> None:
        assert caps.validate_eks_capabilities_config(None) == caps.EKS_CAPABILITIES_DEFAULTS


class TestValidationRejects:
    """Every malformed shape fails loudly with a path-qualified message."""

    def _reject(self, raw: object, match: str, regions: list[str] | None = None) -> None:
        with pytest.raises(caps.EksCapabilitiesConfigError, match=match):
            caps.validate_eks_capabilities_config(raw, regions)

    def test_unknown_top_level_key(self) -> None:
        self._reject(
            {"flux": {"enabled": True}}, r"eks_capabilities contains unknown key\(s\): flux"
        )

    def test_the_retired_argocd_block_is_rejected_by_name(self) -> None:
        """A cdk.json still carrying the hosted Argo CD block fails the synth, not silently."""
        self._reject(
            {"argocd": {"enabled": True}}, r"eks_capabilities contains unknown key\(s\): argocd"
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

    def test_string_lists_reject_blank_entries(self) -> None:
        self._reject(
            {"ack": {"disabled_services": ["ec2", " "]}},
            r"eks_capabilities\.ack\.disabled_services must not contain empty strings",
        )

    def test_validate_rejects_a_non_object_block_like_normalize(self) -> None:
        self._reject(["kro"], r"eks_capabilities must be an object, got list")

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

    @pytest.mark.parametrize(
        "arn",
        [
            "AmazonSQSFullAccess",
            "arn:aws:iam::aws:role/AmazonSQSFullAccess",
            f"arn:aws:iam::{_ACCOUNT}:role/not-a-policy",
            "arn:aws:iam::12345:policy/short-account",
            "arn:aws:s3:::bucket",
        ],
    )
    def test_ack_iam_policy_arns_must_be_managed_policy_arns(self, arn: str) -> None:
        self._reject(
            {"ack": {"iam_policy_arns": [arn]}},
            r"ack\.iam_policy_arns\[0\] must be a managed IAM policy ARN",
        )

    def test_ack_iam_policy_arns_is_a_string_list(self) -> None:
        self._reject(
            {"ack": {"iam_policy_arns": _ACK_AWS_POLICY}},
            r"ack\.iam_policy_arns must be a list of strings",
        )


class TestValidationAccepts:
    def test_full_block_round_trips_with_defaults_filled(self) -> None:
        raw = {
            "ack": {
                "enabled": True,
                "disabled_services": ["ec2"],
                "enable_cross_namespace": True,
                "assume_role_arns": [_ACK_TARGET_ROLE],
                "iam_policy_arns": [
                    _ACK_AWS_POLICY,
                    _ACK_CUSTOMER_POLICY,
                    "arn:aws-us-gov:iam::aws:policy/AmazonS3FullAccess",
                ],
            },
            "kro": {"enabled": True, "regions": [_OTHER_REGION]},
        }
        config = caps.validate_eks_capabilities_config(raw, [_REGION, _OTHER_REGION])
        assert config["ack"]["assume_role_arns"] == [_ACK_TARGET_ROLE]
        assert config["ack"]["iam_policy_arns"][1] == _ACK_CUSTOMER_POLICY
        assert config["kro"] == {"enabled": True, "regions": [_OTHER_REGION]}
        # Validation never mutates the caller's block.
        assert raw["kro"] == {"enabled": True, "regions": [_OTHER_REGION]}


class TestRegionHelpers:
    _CONFIG = caps.normalize_eks_capabilities_config(
        {
            "ack": {"enabled": True},  # every region
            "kro": {"enabled": True, "regions": [_REGION]},
        }
    )

    def test_empty_regions_means_every_region(self) -> None:
        assert caps.capability_enabled_in_region(self._CONFIG, "ack", _REGION)
        assert caps.capability_enabled_in_region(self._CONFIG, "ack", "eu-west-1")

    def test_named_regions_restrict(self) -> None:
        assert caps.capability_enabled_in_region(self._CONFIG, "kro", _REGION)
        assert not caps.capability_enabled_in_region(self._CONFIG, "kro", _OTHER_REGION)

    def test_disabled_type_is_off_everywhere_even_when_regions_name_it(self) -> None:
        config = caps.normalize_eks_capabilities_config(
            {"kro": {"enabled": False, "regions": [_REGION]}}
        )
        assert not caps.capability_enabled_in_region(config, "kro", _REGION)

    def test_enabled_types_keep_canonical_order(self) -> None:
        assert caps.enabled_capability_types(self._CONFIG, _REGION) == ["ack", "kro"]
        assert caps.enabled_capability_types(self._CONFIG, _OTHER_REGION) == ["ack"]
        assert caps.enabled_capability_types(caps.EKS_CAPABILITIES_DEFAULTS, _REGION) == []

    def test_helpers_tolerate_a_malformed_block(self) -> None:
        assert not caps.capability_enabled_in_region({"kro": "yes"}, "kro", _REGION)
        assert not caps.capability_enabled_in_region({"kro": {"enabled": "true"}}, "kro", _REGION)


class TestKroIdentity:
    def test_the_kubernetes_user_is_the_kro_assumed_role_session(self) -> None:
        assert (
            caps.kro_kubernetes_username(partition="aws", account=_ACCOUNT, role_name="kro-role")
            == f"arn:aws:sts::{_ACCOUNT}:assumed-role/kro-role/KRO"
        )
        assert caps.KRO_SESSION_NAME == "KRO"

    def test_replacements_gate_on_the_kro_user(self) -> None:
        assert caps.compute_eks_capabilities_replacements(kro_username=None) == {}
        user = f"arn:aws:sts::{_ACCOUNT}:assumed-role/kro-role/KRO"
        assert caps.compute_eks_capabilities_replacements(kro_username=user) == {
            "{{KRO_CAPABILITY_USERNAME}}": user
        }

    def test_manifest_tokens_are_exactly_the_ones_the_manifest_carries(self) -> None:
        tokens = set(re.findall(r"\{\{[A-Z0-9_]+\}\}", _KRO_MANIFEST.read_text(encoding="utf-8")))
        assert tokens == set(caps.EKS_CAPABILITIES_MANIFEST_TOKENS)


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
        assert config["ack"]["enabled"] is False

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
            match=r"eks_capabilities\.ack\.iam_policy_arns\[0\] must be a managed IAM policy ARN",
        ):
            _loader(valid_cdk_context, {"ack": {"enabled": True, "iam_policy_arns": ["x"]}})

    def test_region_subsets_are_checked_against_the_regional_deployment_regions(
        self, valid_cdk_context
    ) -> None:
        with pytest.raises(
            ConfigValidationError, match="not regional deployment regions: eu-west-1"
        ):
            _loader(valid_cdk_context, {"kro": {"enabled": True, "regions": ["eu-west-1"]}})
        loader = _loader(valid_cdk_context, {"kro": {"enabled": True, "regions": ["us-west-2"]}})
        assert loader.get_eks_capabilities_config()["kro"]["regions"] == ["us-west-2"]

    def test_getter_revalidates_and_wraps_the_module_error(self, valid_cdk_context) -> None:
        """Construction validates once; the getter validates again and speaks the loader's type."""
        loader = _loader(valid_cdk_context)
        with (
            patch.object(
                loader, "_raw_eks_capabilities_config", return_value={"kro": {"enabled": "yes"}}
            ),
            pytest.raises(
                ConfigValidationError, match=r"eks_capabilities\.kro\.enabled must be a boolean"
            ),
        ):
            loader.get_eks_capabilities_config()

    def test_run_scoped_overrides_context_is_deep_merged(self, valid_cdk_context) -> None:
        """``--context eks_capabilities_overrides=<json>`` enables types without editing cdk.json."""
        context = dict(valid_cdk_context)
        context[caps.EKS_CAPABILITIES_CONTEXT_KEY] = {"ack": {"disabled_services": ["ec2"]}}
        context[caps.EKS_CAPABILITIES_OVERRIDES_CONTEXT_KEY] = json.dumps(
            {
                "kro": {"enabled": True},
                "ack": {"enabled": True, "iam_policy_arns": [_ACK_AWS_POLICY]},
            }
        )
        config = ConfigLoader(cdk.App(context=context)).get_eks_capabilities_config()
        assert config["kro"]["enabled"] is True
        assert config["ack"]["enabled"] is True
        assert config["ack"]["disabled_services"] == ["ec2"]  # cdk.json value survives
        assert config["ack"]["iam_policy_arns"] == [_ACK_AWS_POLICY]

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
            {"ack": {"disabled_services": ["ec2"], "regions": ["us-east-1"]}},
            {"ack": {"enabled": True, "regions": []}},
        )
        assert merged == {"ack": {"disabled_services": ["ec2"], "enabled": True, "regions": []}}
        # A malformed block is left for validation to name.
        assert caps.merge_eks_capabilities_overrides("oops", {"kro": {}}) == "oops"


# ─── The kro tenant RBAC manifest ────────────────────────────────────────────


def _kro_documents() -> list[dict[str, Any]]:
    return [doc for doc in yaml.safe_load_all(_KRO_MANIFEST.read_text(encoding="utf-8")) if doc]


class TestKroTenantAccessManifest:
    def test_every_binding_names_the_kro_user_and_nothing_else(self) -> None:
        bindings = [
            doc for doc in _kro_documents() if doc["kind"] in {"RoleBinding", "ClusterRoleBinding"}
        ]
        assert bindings
        for binding in bindings:
            assert binding["subjects"] == [
                {
                    "kind": "User",
                    "name": "{{KRO_CAPABILITY_USERNAME}}",
                    "apiGroup": "rbac.authorization.k8s.io",
                }
            ]

    def test_writes_are_confined_to_the_tenant_namespaces(self) -> None:
        roles = [doc for doc in _kro_documents() if doc["kind"] == "Role"]
        assert sorted(doc["metadata"]["namespace"] for doc in roles) == [
            "gco-inference",
            "gco-jobs",
        ]
        cluster_roles = [doc for doc in _kro_documents() if doc["kind"] == "ClusterRole"]
        for cluster_role in cluster_roles:
            for rule in cluster_role["rules"]:
                assert set(rule["verbs"]) <= {"get", "list", "watch"}, rule

    def test_no_guardrail_kind_and_no_wildcard_is_writable(self) -> None:
        guardrails = {"resourcequotas", "limitranges", "networkpolicies", "roles", "rolebindings"}
        for role in (doc for doc in _kro_documents() if doc["kind"] == "Role"):
            for rule in role["rules"]:
                assert "*" not in rule["resources"] and "*" not in rule["apiGroups"], rule
                assert not guardrails & set(rule["resources"]), rule
                assert not {"escalate", "bind", "impersonate", "*"} & set(rule["verbs"]), rule


# ─── Regional stack synthesis ────────────────────────────────────────────────

_EVERYTHING_ON: dict[str, Any] = caps.normalize_eks_capabilities_config(
    {
        "ack": {
            "enabled": True,
            "disabled_services": ["ec2", "rds"],
            "enable_cross_namespace": True,
            "assume_role_arns": [_ACK_TARGET_ROLE, _ACK_TARGET_PATTERN],
            "iam_policy_arns": [_ACK_AWS_POLICY, _ACK_CUSTOMER_POLICY],
        },
        "kro": {"enabled": True},
    }
)

# ACK selected for this region, kro for another: proves the per-type
# ``regions`` subset and that only ACK's resources appear.
_SUBSET: dict[str, Any] = caps.normalize_eks_capabilities_config(
    {
        "ack": {"enabled": True, "regions": [_REGION]},
        "kro": {"enabled": True, "regions": [_OTHER_REGION]},
    }
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


def _cluster_logical_id(template: dict[str, Any]) -> str:
    clusters = list(_resources(template, "AWS::EKS::Cluster"))
    assert len(clusters) == 1
    return clusters[0]


class TestShippedDefaultSynthesizesNothing:
    def test_no_capability_or_role_resources(self, all_off) -> None:
        _stack, template = all_off
        assert _capabilities(template) == {}
        assert _capability_roles(template) == {}
        assert not [key for key in template.get("Outputs", {}) if key.startswith("EksCapability")]

    def test_no_capability_tokens(self, all_off) -> None:
        _stack, template = all_off
        replacements = _replacements(template)
        assert not [key for key in replacements if key.startswith(("{{KRO_", "{{ARGOCD_CAP"))]
        # The hosted Argo CD's cluster-ARN token is gone with it.
        assert "{{EKS_CLUSTER_ARN}}" not in replacements

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


class TestEverythingOn:
    def test_one_capability_per_type_with_deterministic_names(self, everything_on) -> None:
        _stack, template = everything_on
        capabilities = _capabilities(template)
        assert set(capabilities) == {"EksCapabilityAck", "EksCapabilityKro"}
        cluster = _cluster_logical_id(template)
        expected_types = {"EksCapabilityAck": "ACK", "EksCapabilityKro": "KRO"}
        expected_names = {"EksCapabilityAck": "gco-test-ack", "EksCapabilityKro": "gco-test-kro"}
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
        assert len(roles) == 2
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

    def test_ack_role_holds_exactly_the_configured_managed_policies(self, everything_on) -> None:
        """AWS managed policies follow the stack's partition; customer ones stay verbatim."""
        _stack, template = everything_on
        role = _capability_roles(template)[_role_logical_id(template, "Ack")]
        assert role["Properties"]["ManagedPolicyArns"] == [
            {
                "Fn::Join": [
                    "",
                    ["arn:", {"Ref": "AWS::Partition"}, ":iam::aws:policy/AmazonSQSFullAccess"],
                ]
            },
            _ACK_CUSTOMER_POLICY,
        ]

    def test_kro_role_has_no_aws_permissions(self, everything_on) -> None:
        _stack, template = everything_on
        role = _capability_roles(template)[_role_logical_id(template, "Kro")]
        assert "Policies" not in role["Properties"]
        assert "ManagedPolicyArns" not in role["Properties"]

    def test_only_operator_configured_grants_are_acknowledged_for_cdk_nag(
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

        ack = acknowledged("EksCapabilityAckRole")
        assert f"AwsSolutions-IAM5[Resource::{_ACK_TARGET_PATTERN}]" in ack
        assert (
            "AwsSolutions-IAM4[Policy::arn:<AWS::Partition>:iam::aws:policy/AmazonSQSFullAccess]"
            in ack
        )
        assert not [finding for finding in ack if "Resource::*]" in finding]
        assert acknowledged("EksCapabilityKroRole") == {}

    def test_outputs(self, everything_on) -> None:
        _stack, template = everything_on
        outputs = {
            key: value
            for key, value in template["Outputs"].items()
            if key.startswith("EksCapability")
        }
        assert set(outputs) == {
            "EksCapabilityAckArn",
            "EksCapabilityAckRoleArn",
            "EksCapabilityKroArn",
            "EksCapabilityKroRoleArn",
        }
        assert outputs["EksCapabilityAckArn"]["Value"] == {
            "Fn::GetAtt": ["EksCapabilityAck", "Arn"]
        }
        assert outputs["EksCapabilityKroRoleArn"]["Value"] == {
            "Fn::GetAtt": [_role_logical_id(template, "Kro"), "Arn"]
        }

    def test_stack_registries(self, everything_on) -> None:
        stack, _template = everything_on
        assert list(stack.eks_capabilities) == ["ack", "kro"]
        assert list(stack.eks_capability_roles) == ["ack", "kro"]
        assert stack.eks_capabilities_config == _EVERYTHING_ON

    def test_kro_token_names_the_capability_session_of_the_generated_role(
        self, everything_on
    ) -> None:
        _stack, template = everything_on
        username = _replacements(template)["{{KRO_CAPABILITY_USERNAME}}"]
        rendered = json.dumps(username)
        assert _role_logical_id(template, "Kro") in rendered
        assert "assumed-role/" in rendered and "/KRO" in rendered
        assert '"Ref": "AWS::Partition"' in rendered or "arn:aws:sts::" in rendered

    def test_convergence_does_not_wait_for_the_capabilities(self, everything_on) -> None:
        """Nothing the applier renders needs a capability's CRDs (Argo CD is Helm-installed)."""
        _stack, template = everything_on
        depends_on = set(_trigger(template).get("DependsOn") or [])
        assert not {"EksCapabilityAck", "EksCapabilityKro"} & depends_on


class TestPerRegionSubset:
    def test_only_the_types_selected_for_this_region_synthesize(self, subset) -> None:
        stack, template = subset
        assert set(_capabilities(template)) == {"EksCapabilityAck"}
        assert set(_capability_roles(template)) == {_role_logical_id(template, "Ack")}
        assert list(stack.eks_capabilities) == ["ack"]
        assert "EksCapabilityKro" not in template["Resources"]

    def test_ack_without_grants_has_no_policy(self, subset) -> None:
        _stack, template = subset
        role = _capability_roles(template)[_role_logical_id(template, "Ack")]
        assert "Policies" not in role["Properties"]
        assert "ManagedPolicyArns" not in role["Properties"]
        assert _capabilities(template)["EksCapabilityAck"]["Properties"]["Configuration"] == {
            "Ack": {"EnableCrossNamespace": False}
        }

    def test_kro_selected_elsewhere_emits_no_kro_token_here(self, subset) -> None:
        _stack, template = subset
        assert "{{KRO_CAPABILITY_USERNAME}}" not in _replacements(template)
