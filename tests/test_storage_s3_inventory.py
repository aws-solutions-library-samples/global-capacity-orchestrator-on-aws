"""`gco storage s3-inventory` — the full S3 bucket inventory.

``storage list`` deliberately reports only the four buckets addressable by
``storage sync``. ``storage s3-inventory`` answers the broader operator question:
what buckets does this deployment create, which can my job pods write to, how do
pods discover them, and what does teardown do to them?

The properties worth pinning:

* **Completeness.** Every descriptor produces an entry, and a bucket whose stack
  is not deployed is reported as ``not-deployed`` rather than omitted — a silently
  short inventory would read as "this deployment has no cost bucket" when the
  monitoring stack simply has not rolled out yet.
* **Honest failure.** A missing stack is an expected state (``{}``); a permissions
  or transport error must propagate, never be reported as "not deployed".
* **Physical identity is resolved, not reconstructed.** Names come from the SSM
  and CloudFormation contracts the stacks publish.
* **Call economy.** One CloudFormation sweep per stack serves every access-logs
  entry in it, so adding regions scales linearly rather than quadratically.
* **The pod-access answer is correct.** ``summary.pod_writable`` is exactly the
  buckets the job-pod role can write to, which is what a caller deciding where to
  put checkpoints actually reads.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from botocore.exceptions import ClientError

from cli.config import GCOConfig
from cli.storage import BUCKET_DESCRIPTORS, StorageManager

_ACCOUNT = "123456789012"

_SSM_VALUES = {
    "/gco/cluster-shared-bucket/name": "gco-cluster-shared-123456789012-us-east-2",
    "/gco/cluster-shared-bucket/arn": "arn:aws:s3:::gco-cluster-shared-123456789012-us-east-2",
    "/gco/regional-shared-bucket/name": "gco-regional-shared-123456789012-us-east-1",
    "/gco/regional-shared-bucket/arn": "arn:aws:s3:::gco-regional-shared-123456789012-us-east-1",
    "/gco/model-bucket-name": "gco-models-123456789012-us-east-2",
}

#: logical-id prefix -> physical name, per (stack, region). The analytics stack is
#: deliberately absent: it is opt-in and normally not deployed.
_STACK_SWEEPS = {
    ("gco-global", "us-east-2"): {
        "ClusterSharedBucket": "gco-cluster-shared-123456789012-us-east-2",
        "ClusterSharedAccessLogsBucket": "gco-global-clustersharedaccesslogs-abc123",
        "ModelWeightsBucket": "gco-models-123456789012-us-east-2",
        "ModelWeightsAccessLogsBucket": "gco-global-modelweightsaccesslogs-def456",
    },
    ("gco-us-east-1", "us-east-1"): {
        "RegionalSharedBucket": "gco-regional-shared-123456789012-us-east-1",
        "RegionalSharedAccessLogsBucket": "gco-us-east-1-regionalshared-ghi789",
    },
    ("gco-monitoring", "us-east-2"): {
        "CostReportBucket": "gco-cost-reports-123456789012-us-east-2",
        "CostReportAccessLogsBucket": "gco-monitoring-costreportaccesslogs-jkl012",
    },
}


def _config(regional: list[str] | None = None) -> GCOConfig:
    config = GCOConfig(
        project_name="gco",
        global_region="us-east-2",
        monitoring_region="us-east-2",
        api_gateway_region="us-east-2",
        default_region=(regional or ["us-east-1"])[0],
    )
    config._apply_project_scoped_names()
    return config


def _inventory(regional: list[str] | None = None, **kwargs: Any) -> dict[str, Any]:
    """Build an inventory with every AWS call stubbed."""
    regions = regional or ["us-east-1"]
    with (
        patch(
            "gco.services.aws_ssm.get_ssm_parameter_optional",
            side_effect=lambda name, region=None: _SSM_VALUES.get(name),
        ),
        patch.object(StorageManager, "_account_id", return_value=_ACCOUNT),
        patch.object(StorageManager, "_configured_regional_regions", return_value=regions),
        patch.object(
            StorageManager,
            "_stack_bucket_resources",
            side_effect=lambda stack, region: _STACK_SWEEPS.get((stack, region), {}),
        ),
    ):
        return StorageManager(_config(regions)).s3_inventory(**kwargs)


class TestInventoryCompleteness:
    def test_every_descriptor_produces_an_entry(self) -> None:
        inventory = _inventory()
        assert len(inventory["buckets"]) == len(BUCKET_DESCRIPTORS)

    def test_undeployed_buckets_are_reported_not_omitted(self) -> None:
        """A short inventory would read as "this deployment has no such bucket"."""
        inventory = _inventory()
        undeployed = {
            item["id"] for item in inventory["buckets"] if item["status"] == "not-deployed"
        }
        assert undeployed == {"analytics-studio", "analytics-studio-access-logs"}

    def test_undeployed_entries_explain_themselves(self) -> None:
        inventory = _inventory()
        entry = next(item for item in inventory["buckets"] if item["id"] == "analytics-studio")
        assert "not deployed" in entry["detail"]
        # An opt-in bucket names its toggle so the reader knows it is absent by
        # choice rather than broken.
        assert entry["opt_in"] == "analytics_environment.enabled"
        assert "analytics_environment.enabled" in entry["detail"]

    def test_summary_counts_agree_with_the_entries(self) -> None:
        inventory = _inventory()
        summary = inventory["summary"]
        assert summary["total"] == len(inventory["buckets"])
        assert summary["deployed"] + summary["not_deployed"] == summary["total"]
        assert summary["deployed"] == 8

    def test_one_entry_per_region_for_regional_buckets(self) -> None:
        regions = ["us-east-1", "us-west-2"]
        sweeps = dict(_STACK_SWEEPS)
        sweeps[("gco-us-west-2", "us-west-2")] = {
            "RegionalSharedBucket": "gco-regional-shared-123456789012-us-west-2",
            "RegionalSharedAccessLogsBucket": "gco-us-west-2-regionalshared-zzz",
        }
        with patch.dict(_STACK_SWEEPS, sweeps, clear=False):
            inventory = _inventory(regional=regions)

        ids = [item["id"] for item in inventory["buckets"]]
        assert "regional-shared:us-east-1" in ids
        assert "regional-shared:us-west-2" in ids
        assert inventory["regions"]["regional"] == regions


class TestPodAccessAnswer:
    def test_pod_writable_lists_exactly_the_read_write_buckets(self) -> None:
        """This is the field a caller reads to decide where checkpoints go."""
        inventory = _inventory()
        assert inventory["summary"]["pod_writable"] == [
            "gco-cluster-shared-123456789012-us-east-2",
            "gco-regional-shared-123456789012-us-east-1",
        ]

    def test_access_log_buckets_are_never_pod_accessible(self) -> None:
        inventory = _inventory()
        for item in inventory["buckets"]:
            if item["role"] == "access-logs":
                assert item["pod_access"] == "none"

    def test_model_bucket_is_read_only_to_pods(self) -> None:
        """The job-pod role's gco-* wildcard grants reads only."""
        inventory = _inventory()
        entry = next(item for item in inventory["buckets"] if item["id"] == "model-weights")
        assert entry["pod_access"] == "read-only"

    def test_pod_writable_buckets_name_their_discovery_surface(self) -> None:
        """A writable bucket a pod cannot find the name of is not usable."""
        inventory = _inventory()
        for item in inventory["buckets"]:
            if item["pod_access"] == "read-write":
                assert "ConfigMap" in item["discovery"]


class TestPhysicalIdentityResolution:
    def test_shared_buckets_take_name_and_arn_from_ssm(self) -> None:
        inventory = _inventory()
        entry = next(item for item in inventory["buckets"] if item["id"] == "cluster-shared")
        assert entry["bucket"] == _SSM_VALUES["/gco/cluster-shared-bucket/name"]
        assert entry["arn"] == _SSM_VALUES["/gco/cluster-shared-bucket/arn"]
        assert entry["s3_uri"] == f"s3://{entry['bucket']}/"

    def test_access_log_names_come_from_cloudformation(self) -> None:
        """They are CDK-auto-named, so only the stack knows them."""
        inventory = _inventory()
        entry = next(
            item for item in inventory["buckets"] if item["id"] == "cluster-shared-access-logs"
        )
        assert entry["bucket"] == "gco-global-clustersharedaccesslogs-abc123"

    def test_arn_is_synthesized_only_when_ssm_does_not_publish_one(self) -> None:
        inventory = _inventory()
        entry = next(item for item in inventory["buckets"] if item["id"] == "model-weights")
        assert entry["arn"] == f"arn:aws:s3:::{entry['bucket']}"

    def test_regional_entries_carry_their_own_region_and_stack(self) -> None:
        inventory = _inventory()
        entry = next(
            item for item in inventory["buckets"] if item["id"] == "regional-shared:us-east-1"
        )
        assert entry["region"] == "us-east-1"
        assert entry["owning_stack"] == "gco-us-east-1"
        assert entry["sync_alias"] == "regional-shared:us-east-1"


class TestStackSweepBehavior:
    """_stack_bucket_resources is the CloudFormation half; test it directly."""

    def test_missing_stack_is_an_empty_result_not_an_error(self) -> None:
        manager = StorageManager(_config())
        error = ClientError(
            {"Error": {"Code": "ValidationError", "Message": "Stack with id x does not exist"}},
            "ListStackResources",
        )
        with patch("cli.storage.boto3.client") as client:
            client.return_value.list_stack_resources.side_effect = error
            assert manager._stack_bucket_resources("gco-analytics", "us-east-2") == {}

    def test_permission_error_propagates(self) -> None:
        """An AccessDenied must never be reported to the operator as "not deployed"."""
        manager = StorageManager(_config())
        error = ClientError(
            {"Error": {"Code": "AccessDenied", "Message": "nope"}}, "ListStackResources"
        )
        with patch("cli.storage.boto3.client") as client:
            client.return_value.list_stack_resources.side_effect = error
            with pytest.raises(ClientError):
                manager._stack_bucket_resources("gco-global", "us-east-2")

    def test_matches_logical_ids_by_construct_prefix(self) -> None:
        """CDK appends a hash, so matching is by the stable construct-id prefix."""
        manager = StorageManager(_config())
        with patch("cli.storage.boto3.client") as client:
            client.return_value.list_stack_resources.return_value = {
                "StackResourceSummaries": [
                    {
                        "ResourceType": "AWS::S3::Bucket",
                        "LogicalResourceId": "RegionalSharedBucket3FF19783",
                        "PhysicalResourceId": "gco-regional-shared-123456789012-us-east-1",
                    },
                    {
                        "ResourceType": "AWS::IAM::Role",
                        "LogicalResourceId": "RegionalSharedBucketRole",
                        "PhysicalResourceId": "not-a-bucket",
                    },
                ]
            }
            resources = manager._stack_bucket_resources("gco-us-east-1", "us-east-1")

        assert resources == {"RegionalSharedBucket": "gco-regional-shared-123456789012-us-east-1"}

    def test_paginates(self) -> None:
        manager = StorageManager(_config())
        with patch("cli.storage.boto3.client") as client:
            client.return_value.list_stack_resources.side_effect = [
                {
                    "StackResourceSummaries": [
                        {
                            "ResourceType": "AWS::S3::Bucket",
                            "LogicalResourceId": "ClusterSharedBucketAAA",
                            "PhysicalResourceId": "primary",
                        }
                    ],
                    "NextToken": "more",
                },
                {
                    "StackResourceSummaries": [
                        {
                            "ResourceType": "AWS::S3::Bucket",
                            "LogicalResourceId": "ClusterSharedAccessLogsBucketBBB",
                            "PhysicalResourceId": "logs",
                        }
                    ]
                },
            ]
            resources = manager._stack_bucket_resources("gco-global", "us-east-2")

        assert resources == {
            "ClusterSharedBucket": "primary",
            "ClusterSharedAccessLogsBucket": "logs",
        }

    def test_result_is_cached_per_stack_and_region(self) -> None:
        """One sweep per stack serves every access-logs entry in it."""
        manager = StorageManager(_config())
        with patch("cli.storage.boto3.client") as client:
            client.return_value.list_stack_resources.return_value = {"StackResourceSummaries": []}
            manager._stack_bucket_resources("gco-global", "us-east-2")
            manager._stack_bucket_resources("gco-global", "us-east-2")
            assert client.return_value.list_stack_resources.call_count == 1


class TestDescriptorTableIntegrity:
    def test_ids_are_unique(self) -> None:
        ids = [item.id for item in BUCKET_DESCRIPTORS]
        assert len(ids) == len(set(ids))

    def test_every_primary_bucket_has_an_access_logs_sibling(self) -> None:
        """The "every bucket must log" control has no exceptions in this repo."""
        primaries = {item.id for item in BUCKET_DESCRIPTORS if item.role == "primary"}
        logs = {item.id for item in BUCKET_DESCRIPTORS if item.role == "access-logs"}
        assert {f"{name}-access-logs" for name in primaries} == logs

    def test_enumerations_are_known_values(self) -> None:
        for item in BUCKET_DESCRIPTORS:
            assert item.role in {"primary", "access-logs"}
            assert item.scope in {"global", "regional", "monitoring", "analytics"}
            assert item.pod_access in {"read-write", "read-only", "none"}
            assert item.removal_policy in {"destroy", "retain"}

    def test_logical_id_prefixes_are_unique(self) -> None:
        """The CloudFormation sweep keys on these, so a collision would alias."""
        prefixes = [item.logical_id_prefix for item in BUCKET_DESCRIPTORS]
        assert len(prefixes) == len(set(prefixes))

    def test_no_prefix_is_a_prefix_of_another(self) -> None:
        """startswith matching would otherwise assign a bucket to the wrong entry."""
        prefixes = [item.logical_id_prefix for item in BUCKET_DESCRIPTORS]
        for candidate in prefixes:
            others = [item for item in prefixes if item != candidate]
            assert not [item for item in others if item.startswith(candidate)], (
                f"{candidate!r} is a prefix of another descriptor's logical id"
            )


class TestInventoryCommand:
    def test_json_output_is_the_whole_document(self) -> None:
        import json

        from click.testing import CliRunner

        from cli.main import cli

        manager = MagicMock()
        manager.s3_inventory.return_value = {
            "project_name": "gco",
            "account": _ACCOUNT,
            "regions": {"regional": ["us-east-1"]},
            "buckets": [],
            "summary": {"total": 0, "deployed": 0, "not_deployed": 0, "pod_writable": []},
        }
        with patch("cli.storage.get_storage_manager", return_value=manager):
            result = CliRunner().invoke(cli, ["-o", "json", "storage", "s3-inventory"])

        assert result.exit_code == 0
        assert json.loads(result.output)["account"] == _ACCOUNT

    def test_region_option_is_forwarded(self) -> None:
        from click.testing import CliRunner

        from cli.main import cli

        manager = MagicMock()
        manager.s3_inventory.return_value = {
            "project_name": "gco",
            "account": _ACCOUNT,
            "regions": {"regional": ["us-west-2"]},
            "buckets": [],
            "summary": {"total": 0, "deployed": 0, "not_deployed": 0, "pod_writable": []},
        }
        with patch("cli.storage.get_storage_manager", return_value=manager):
            result = CliRunner().invoke(
                cli, ["-o", "json", "storage", "s3-inventory", "--region", "us-west-2"]
            )

        assert result.exit_code == 0
        manager.s3_inventory.assert_called_once_with(region="us-west-2")

    def test_failure_exits_nonzero_with_a_message(self) -> None:
        from click.testing import CliRunner

        from cli.main import cli

        manager = MagicMock()
        manager.s3_inventory.side_effect = RuntimeError("boom")
        with patch("cli.storage.get_storage_manager", return_value=manager):
            result = CliRunner().invoke(cli, ["-o", "json", "storage", "s3-inventory"])

        assert result.exit_code == 1
        assert "boom" in result.output
