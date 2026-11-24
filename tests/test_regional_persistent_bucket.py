"""Each workload Region's persistent bucket, which ``gco upgrade`` leaves standing.

``cdk.json::regional_persistent_bucket`` adds a ``<project>-<region>-persistent``
stack per workload Region. It sits in the control-plane tier, so the teardown
of ``gco upgrade`` and ``gco stacks destroy-all --keep-control-plane`` keeps
it, and the regional stack of the same Region reads the bucket's identity from
SSM to grant its job pods access. These tests pin:

* the configuration block: off when it is absent (a cdk.json from v9.1.0,
  whose upgrade runs the previous release's CLI), on in the shipped cdk.json,
  ``expiration_days`` null or a positive integer, and any other value rejected
  while the app loads;
* the persistent stack's template: a versioned bucket encrypted with its own
  KMS key that deletes with the stack and takes its objects along, its
  access-logs bucket, the lifecycle rules for both forms of
  ``expiration_days``, and the four SSM parameters, with no export another
  stack could import;
* the regional stack: with the bucket on, four SSM-backed template parameters,
  read/write on exactly that bucket and its key for the job-pod role, and the
  three ConfigMap replacements; with it off, none of them;
* the CLI: deploy-all deploys the persistent stacks with the global tier,
  before the regional stacks; a full destroy-all deletes them after the
  regional stacks; the keep-control-plane teardown and ``gco upgrade`` keep
  them.
"""

from __future__ import annotations

import json
import types
from functools import cache
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import aws_cdk as cdk
import pytest
from aws_cdk import assertions

from cli.stacks import (
    StackManager,
    _is_pre_regional_stack,
    _persistent_stack_region,
    get_stack_deployment_order,
    get_stack_destroy_order,
)
from cli.upgrade import split_stacks
from gco.config.config_loader import ConfigLoader, ConfigValidationError
from gco.stacks.constants import regional_persistent_ssm_parameter_prefix
from gco.stacks.regional_persistent_stack import (
    INCOMPLETE_UPLOAD_RETENTION_DAYS,
    NONCURRENT_VERSION_RETENTION_DAYS,
    GCORegionalPersistentStack,
    regional_persistent_stack_name,
)
from gco.stacks.regional_stack import (
    GCORegionalStack,
    PersistentBucketIdentity,
    _compute_kubectl_regional_persistent_replacements,
)
from tests.test_config_loader import MockApp, minimal_context
from tests.test_mooncake_regional_bucket_synthesis import (
    _pod_role_statements,
    _regional_template_json,
)
from tests.test_regional_shared_configmap import _image_replacements
from tests.test_regional_stack import MockConfigLoader
from tests.test_regional_stack import TestRegionalStackSynthesis as _RegionalStackSynthesisFixtures

ROOT = Path(__file__).resolve().parent.parent
_ACCOUNT = "123456789012"
_REGION = "us-east-1"
_PROJECT = "gco-test"
_PREFIX = regional_persistent_ssm_parameter_prefix(_PROJECT)

_BUCKET_ACTIONS = {
    "s3:GetObject",
    "s3:PutObject",
    "s3:DeleteObject",
    "s3:ListBucket",
    "s3:GetBucketLocation",
}
_KEY_ACTIONS = {"kms:Decrypt", "kms:Encrypt", "kms:GenerateDataKey", "kms:DescribeKey"}
_PLACEHOLDERS = (
    "{{REGIONAL_PERSISTENT_BUCKET}}",
    "{{REGIONAL_PERSISTENT_BUCKET_ARN}}",
    "{{REGIONAL_PERSISTENT_BUCKET_REGION}}",
)


def _shipped_context() -> dict[str, Any]:
    return json.loads((ROOT / "cdk.json").read_text(encoding="utf-8"))["context"]


def _read_block(block: object) -> dict[str, Any]:
    """``get_regional_persistent_bucket_config`` on a context holding only ``block``."""
    loader = ConfigLoader.__new__(ConfigLoader)
    context = {"regional_persistent_bucket": block}
    loader.app = types.SimpleNamespace(  # type: ignore[assignment]
        node=types.SimpleNamespace(try_get_context=context.get)
    )
    return loader.get_regional_persistent_bucket_config()


class TestConfiguration:
    def test_the_shipped_cdk_json_turns_it_on_and_keeps_objects(self) -> None:
        context = _shipped_context()
        assert context["regional_persistent_bucket"] == {"enabled": True, "expiration_days": None}
        assert context["_comment_regional_persistent_bucket"]
        assert _read_block(context["regional_persistent_bucket"]) == {
            "enabled": True,
            "expiration_days": None,
        }

    @pytest.mark.parametrize("block", [None, {}, {"expiration_days": 30}])
    def test_an_absent_block_or_flag_is_off(self, block: object) -> None:
        """A cdk.json from v9.1.0 has no block, and its upgrade runs v9.1.0's CLI.

        That CLI reads ``<project>-<region>-persistent`` as a regional stack in
        a Region named ``<region>-persistent``, so the stacks must not appear
        until the operator adds the block after the upgrade.
        """
        assert _read_block(block)["enabled"] is False

    def test_expiration_days_is_passed_through(self) -> None:
        assert _read_block({"enabled": True, "expiration_days": 14}) == {
            "enabled": True,
            "expiration_days": 14,
        }

    @pytest.mark.parametrize(
        ("block", "message"),
        [
            (True, "must be a JSON object"),
            (["enabled"], "must be a JSON object"),
            ({"enabled": "true"}, r"enabled must be true or false"),
            ({"enabled": 1}, r"enabled must be true or false"),
            ({"enabled": True, "expiration_days": 0}, "positive whole number"),
            ({"enabled": True, "expiration_days": -7}, "positive whole number"),
            ({"enabled": True, "expiration_days": True}, "positive whole number"),
            ({"enabled": True, "expiration_days": 1.5}, "positive whole number"),
            ({"enabled": True, "expiration_days": "30"}, "positive whole number"),
        ],
    )
    def test_other_values_are_rejected(self, block: object, message: str) -> None:
        with pytest.raises(ConfigValidationError, match=message):
            _read_block(block)

    def test_loading_the_app_validates_the_block(self) -> None:
        context = {**minimal_context(), "regional_persistent_bucket": {"enabled": "yes"}}
        with pytest.raises(ConfigValidationError, match=r"regional_persistent_bucket\.enabled"):
            ConfigLoader(MockApp(context))

    def test_unknown_keys_are_rejected(self) -> None:
        context = {
            **minimal_context(),
            "regional_persistent_bucket": {"enabled": True, "retention_days": 7},
        }
        with pytest.raises(ConfigValidationError, match="retention_days"):
            ConfigLoader(MockApp(context))


class _PersistentStackConfig:
    """The ConfigLoader methods ``GCORegionalPersistentStack`` reads."""

    def __init__(self, expiration_days: int | None) -> None:
        self._expiration_days = expiration_days

    def get_project_name(self) -> str:
        return _PROJECT

    def get_global_region(self) -> str:
        return "us-east-2"

    def get_regional_persistent_bucket_config(self) -> dict[str, Any]:
        return {"enabled": True, "expiration_days": self._expiration_days}


@cache
def _persistent_template(expiration_days: int | None = None) -> dict[str, Any]:
    stack = GCORegionalPersistentStack(
        cdk.App(),
        regional_persistent_stack_name(_PROJECT, _REGION),
        config=_PersistentStackConfig(expiration_days),  # type: ignore[arg-type]
        region=_REGION,
        env=cdk.Environment(account=_ACCOUNT, region=_REGION),
    )
    return assertions.Template.from_stack(stack).to_json()


def _of_type(template: dict[str, Any], resource_type: str) -> dict[str, dict[str, Any]]:
    return {
        logical_id: resource
        for logical_id, resource in template["Resources"].items()
        if resource["Type"] == resource_type
    }


def _ids(template: dict[str, Any]) -> tuple[str, str, str]:
    """Logical IDs of the persistent bucket, its access-logs bucket and its key."""
    buckets = _of_type(template, "AWS::S3::Bucket")
    (bucket,) = [item for item in buckets if item.startswith("PersistentBucket")]
    (logs,) = [item for item in buckets if item.startswith("PersistentAccessLogsBucket")]
    (key,) = _of_type(template, "AWS::KMS::Key")
    return bucket, logs, key


def _lifecycle_rules(template: dict[str, Any]) -> list[dict[str, Any]]:
    bucket, _, _ = _ids(template)
    properties = template["Resources"][bucket]["Properties"]
    return properties["LifecycleConfiguration"]["Rules"]


class TestPersistentStackTemplate:
    def test_the_region_comes_before_the_suffix(self) -> None:
        """No regional stack or API bridge of any project ends in ``-persistent``."""
        assert regional_persistent_stack_name("gco", "us-east-1") == "gco-us-east-1-persistent"

    def test_bucket_logs_and_key_delete_with_the_stack(self) -> None:
        """A full destroy-all deletes the bucket with every object in it."""
        template = _persistent_template()
        bucket, logs, key = _ids(template)
        resources = template["Resources"]
        for logical_id in (bucket, logs, key):
            assert resources[logical_id]["DeletionPolicy"] == "Delete"
            assert resources[logical_id]["UpdateReplacePolicy"] == "Delete"
        emptied = {
            resource["Properties"]["BucketName"]["Ref"]
            for resource in _of_type(template, "Custom::S3AutoDeleteObjects").values()
        }
        assert emptied == {bucket, logs}
        assert resources[key]["Properties"]["EnableKeyRotation"] is True
        assert resources[key]["Properties"]["PendingWindowInDays"] == 7

    def test_bucket_posture(self) -> None:
        template = _persistent_template()
        bucket, logs, key = _ids(template)
        properties = template["Resources"][bucket]["Properties"]
        # CloudFormation names the bucket, so a redeploy cannot collide (ADR-0005).
        assert "BucketName" not in properties
        assert properties["VersioningConfiguration"] == {"Status": "Enabled"}
        assert properties["PublicAccessBlockConfiguration"] == {
            "BlockPublicAcls": True,
            "BlockPublicPolicy": True,
            "IgnorePublicAcls": True,
            "RestrictPublicBuckets": True,
        }
        assert properties["BucketEncryption"]["ServerSideEncryptionConfiguration"] == [
            {
                "BucketKeyEnabled": True,
                "ServerSideEncryptionByDefault": {
                    "SSEAlgorithm": "aws:kms",
                    "KMSMasterKeyID": {"Fn::GetAtt": [key, "Arn"]},
                },
            }
        ]
        assert properties["LoggingConfiguration"] == {
            "DestinationBucketName": {"Ref": logs},
            "LogFilePrefix": "regional-persistent/",
        }
        (policy,) = [
            resource
            for resource in _of_type(template, "AWS::S3::BucketPolicy").values()
            if resource["Properties"]["Bucket"] == {"Ref": bucket}
        ]
        statements = policy["Properties"]["PolicyDocument"]["Statement"]
        assert "DenyInsecureTransport" in {statement.get("Sid") for statement in statements}

    def test_access_logs_bucket_is_sse_s3_and_expires_logs(self) -> None:
        template = _persistent_template()
        _, logs, _ = _ids(template)
        properties = template["Resources"][logs]["Properties"]
        assert properties["BucketEncryption"]["ServerSideEncryptionConfiguration"] == [
            {"ServerSideEncryptionByDefault": {"SSEAlgorithm": "AES256"}}
        ]
        assert properties["LifecycleConfiguration"]["Rules"] == [
            {"Id": "ExpireAccessLogs", "Status": "Enabled", "ExpirationInDays": 90}
        ]

    def test_by_default_objects_stay_until_they_are_deleted(self) -> None:
        assert (INCOMPLETE_UPLOAD_RETENTION_DAYS, NONCURRENT_VERSION_RETENTION_DAYS) == (7, 30)
        assert _lifecycle_rules(_persistent_template()) == [
            {
                "Id": "AbortIncompleteUploads",
                "Status": "Enabled",
                "AbortIncompleteMultipartUpload": {"DaysAfterInitiation": 7},
            },
            {
                "Id": "ExpireNoncurrentVersions",
                "Status": "Enabled",
                "NoncurrentVersionExpiration": {"NoncurrentDays": 30},
                "ExpiredObjectDeleteMarker": True,
            },
        ]

    def test_expiration_days_expires_current_objects_too(self) -> None:
        rules = _lifecycle_rules(_persistent_template(14))
        assert [rule["Id"] for rule in rules] == [
            "AbortIncompleteUploads",
            "ExpireNoncurrentVersions",
            "ExpireObjects",
        ]
        assert rules[2] == {"Id": "ExpireObjects", "Status": "Enabled", "ExpirationInDays": 14}

    def test_the_identity_is_published_to_ssm_and_nothing_is_exported(self) -> None:
        """The regional stack reads SSM, so no export pins this stack in place."""
        template = _persistent_template()
        bucket, _, key = _ids(template)
        parameters = {
            resource["Properties"]["Name"]: resource["Properties"]["Value"]
            for resource in _of_type(template, "AWS::SSM::Parameter").values()
        }
        assert _PREFIX == "/gco-test/regional-persistent-bucket"
        assert parameters == {
            f"{_PREFIX}/name": {"Ref": bucket},
            f"{_PREFIX}/arn": {"Fn::GetAtt": [bucket, "Arn"]},
            f"{_PREFIX}/region": _REGION,
            f"{_PREFIX}/kms-key-arn": {"Fn::GetAtt": [key, "Arn"]},
        }
        assert not [output for output in template.get("Outputs", {}).values() if "Export" in output]


class _PersistentOnConfigLoader(MockConfigLoader):
    def get_regional_persistent_bucket_config(self) -> dict[str, Any]:
        return {"enabled": True, "expiration_days": None}


@cache
def _regional_stack_with_the_bucket() -> tuple[GCORegionalStack, dict[str, Any]]:
    app = cdk.App()
    with (
        patch("gco.stacks.regional_stack.ecr_assets.DockerImageAsset") as mock_docker,
        patch.object(
            GCORegionalStack,
            "_create_helm_installer_lambda",
            _RegionalStackSynthesisFixtures._mock_helm_installer,
        ),
    ):
        mock_docker.return_value = MagicMock(
            image_uri=f"{_ACCOUNT}.dkr.ecr.{_REGION}.amazonaws.com/test:latest"
        )
        stack = GCORegionalStack(
            app,
            "test-regional-persistent-synthesis",
            config=_PersistentOnConfigLoader(app),
            region=_REGION,
            auth_secret_arn=f"arn:aws:secretsmanager:{_REGION}:{_ACCOUNT}:secret:test-secret",  # nosec B106
            env=cdk.Environment(account=_ACCOUNT, region=_REGION),
        )
        return stack, assertions.Template.from_stack(stack).to_json()


def _persistent_parameters(template: dict[str, Any]) -> dict[str, str]:
    """``<suffix>`` -> logical ID of the template parameter that reads ``<prefix>/<suffix>``."""
    return {
        parameter["Default"].rsplit("/", 1)[1]: logical_id
        for logical_id, parameter in template.get("Parameters", {}).items()
        if parameter.get("Type") == "AWS::SSM::Parameter::Value<String>"
        and str(parameter.get("Default", "")).startswith(f"{_PREFIX}/")
    }


def _actions_granted_on(statements: list[dict[str, Any]], resource: object) -> set[str]:
    actions: set[str] = set()
    for statement in statements:
        resources = statement.get("Resource")
        if statement.get("Effect") != "Allow":
            continue
        if resource in (resources if isinstance(resources, list) else [resources]):
            action = statement["Action"]
            actions.update(action if isinstance(action, list) else [action])
    return actions


class TestRegionalStackWithTheBucket:
    def test_reads_the_four_parameters_when_it_deploys(self) -> None:
        _, template = _regional_stack_with_the_bucket()
        assert sorted(_persistent_parameters(template)) == ["arn", "kms-key-arn", "name", "region"]

    def test_job_pods_get_read_write_on_exactly_that_bucket_and_key(self) -> None:
        _, template = _regional_stack_with_the_bucket()
        parameter = _persistent_parameters(template)
        statements = _pod_role_statements(template)
        bucket_arn = {"Ref": parameter["arn"]}
        objects = {"Fn::Join": ["", [bucket_arn, "/*"]]}
        assert _actions_granted_on(statements, bucket_arn) == _BUCKET_ACTIONS
        assert _actions_granted_on(statements, objects) == _BUCKET_ACTIONS
        assert _actions_granted_on(statements, {"Ref": parameter["kms-key-arn"]}) == _KEY_ACTIONS

    def test_the_configmap_replacements_come_from_the_parameters(self) -> None:
        _, template = _regional_stack_with_the_bucket()
        parameter = _persistent_parameters(template)
        replacements = _image_replacements(template)
        assert {key: replacements[key] for key in _PLACEHOLDERS} == {
            "{{REGIONAL_PERSISTENT_BUCKET}}": {"Ref": parameter["name"]},
            "{{REGIONAL_PERSISTENT_BUCKET_ARN}}": {"Ref": parameter["arn"]},
            "{{REGIONAL_PERSISTENT_BUCKET_REGION}}": {"Ref": parameter["region"]},
        }

    def test_an_acknowledgment_needs_a_template_parameter(self) -> None:
        stack, _ = _regional_stack_with_the_bucket()
        with pytest.raises(ValueError, match="expected a template parameter reference"):
            stack._template_parameter_id("arn:aws:s3:::literal-bucket")


class TestRegionalStackWithoutTheBucket:
    def test_no_parameters_grants_or_replacements(self) -> None:
        """Off, the ConfigMap's placeholders stay unresolved and the applier prunes it."""
        template = _regional_template_json()
        assert _persistent_parameters(template) == {}
        assert "regional-persistent" not in json.dumps(_pod_role_statements(template))
        assert not set(_PLACEHOLDERS) & set(_image_replacements(template))

    def test_the_replacement_helper(self) -> None:
        assert _compute_kubectl_regional_persistent_replacements(None) == {}
        identity = PersistentBucketIdentity(
            name="bucket", arn="arn:aws:s3:::bucket", region="eu-west-1", kms_key_arn="key"
        )
        assert _compute_kubectl_regional_persistent_replacements(identity) == {
            "{{REGIONAL_PERSISTENT_BUCKET}}": "bucket",
            "{{REGIONAL_PERSISTENT_BUCKET_ARN}}": "arn:aws:s3:::bucket",
            "{{REGIONAL_PERSISTENT_BUCKET_REGION}}": "eu-west-1",
        }


_STACKS = [
    "gco-us-west-2",
    "gco-us-west-2-persistent",
    "gco-regional-api-us-east-1",
    "gco-monitoring",
    "gco-us-east-1-persistent",
    "gco-us-east-1",
    "gco-api-gateway",
    "gco-global",
]


class TestStackClassification:
    @pytest.mark.parametrize(
        ("project", "stack", "region"),
        [
            ("gco", "gco-us-east-1-persistent", "us-east-1"),
            ("acme", "acme-ap-southeast-2-persistent", "ap-southeast-2"),
            ("gco-persistent", "gco-persistent-eu-west-1-persistent", "eu-west-1"),
        ],
    )
    def test_the_region_sits_between_project_and_suffix(
        self, project: str, stack: str, region: str
    ) -> None:
        assert _persistent_stack_region(stack, project_name=project) == region
        assert _is_pre_regional_stack(stack, project_name=project)

    @pytest.mark.parametrize(
        ("project", "stack"),
        [
            ("gco", "gco-persistent"),
            ("gco", "gco-us-east-1"),
            ("gco", "other-us-east-1-persistent"),
            # The regional stack of a project named gco-persistent.
            ("gco", "gco-persistent-us-east-1"),
            ("gco-persistent", "gco-persistent-us-east-1"),
        ],
    )
    def test_other_stacks_are_not_persistent(self, project: str, stack: str) -> None:
        assert _persistent_stack_region(stack, project_name=project) is None

    def test_deploy_all_deploys_them_with_the_global_tier(self) -> None:
        assert get_stack_deployment_order(list(_STACKS), project_name="gco") == [
            "gco-global",
            "gco-api-gateway",
            "gco-us-east-1-persistent",
            "gco-us-west-2-persistent",
            "gco-monitoring",
            "gco-us-east-1",
            "gco-us-west-2",
            "gco-regional-api-us-east-1",
        ]

    def test_a_full_destroy_deletes_them_after_the_regional_stacks(self) -> None:
        assert get_stack_destroy_order(list(_STACKS), project_name="gco") == [
            "gco-monitoring",
            "gco-regional-api-us-east-1",
            "gco-us-west-2",
            "gco-us-east-1",
            "gco-us-east-1-persistent",
            "gco-us-west-2-persistent",
            "gco-api-gateway",
            "gco-global",
        ]

    def test_keep_control_plane_and_upgrade_keep_them(self) -> None:
        workload = ["gco-regional-api-us-east-1", "gco-us-west-2", "gco-us-east-1"]
        assert (
            get_stack_destroy_order(list(_STACKS), project_name="gco", keep_control_plane=True)
            == workload
        )
        control_plane, upgrade_workload = split_stacks(list(_STACKS), "gco")
        assert upgrade_workload == workload
        assert control_plane == [
            "gco-us-west-2-persistent",
            "gco-monitoring",
            "gco-us-east-1-persistent",
            "gco-api-gateway",
            "gco-global",
        ]


class TestStackManager:
    @staticmethod
    def _manager() -> StackManager:
        config = MagicMock()
        config.project_name = "gco"
        return StackManager(config)

    def test_deploys_into_its_configured_region(self) -> None:
        manager = self._manager()
        with patch("cli.config._load_cdk_json", return_value={"regional": ["us-east-1"]}):
            assert manager._get_deploy_region("gco-us-east-1-persistent") == "us-east-1"
            # Like a bridge's, the Region must be configured.
            assert manager._get_deploy_region("gco-us-west-2-persistent") is None

    def test_holds_no_cluster_and_mirrors_no_images(self) -> None:
        manager = self._manager()
        assert manager._regional_stack_region("gco-us-east-1-persistent") is None
        assert manager._mirror_target_regions("gco-us-east-1-persistent", False) == []
