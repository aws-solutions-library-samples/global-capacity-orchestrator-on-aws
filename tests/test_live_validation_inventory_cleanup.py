"""Offline coverage for the live-validation inventory, cleanup, and ownership boundary.

Covers ``scripts/live_release_validation/inventory/{_shared,ecr,project,scanners,
stacks}.py``, ``scripts/live_release_validation/cleanup/{ecr,log_groups,retained,
workloads}.py``, and ``scripts/live_release_validation/protected.py``. The
inventory tests pin that every per-service scanner claims a resource only on an
explicit ownership signal (project-prefixed name, CloudFormation stack tag, or
``gco:project`` tag), fails closed on records without an identity, and reports
the unfiltered live authority separately from the owned subset. The protected
tests pin that a pre-existing account resource is matched only by an exact
physical identity in the correct partition, Region, and account, never by prefix
or by a nearby ARN. The cleanup tests pin that deletion authority is re-validated
against the live resource immediately before every destructive request, that
resources whose identity or run tag changed are refused, that ECR residue is
retained rather than deleted, and that log-group and workload convergence keep
every blocked resource visible in the returned evidence. Every boto3 client,
HTTP client, and sleep is faked; nothing here touches AWS.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from contextlib import ExitStack, contextmanager
from datetime import UTC, datetime
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from botocore.exceptions import ClientError

from scripts.live_release_validation import constants, protected
from scripts.live_release_validation.cleanup import ecr as cleanup_ecr
from scripts.live_release_validation.cleanup import log_groups as cleanup_log_groups
from scripts.live_release_validation.cleanup import retained as cleanup_retained
from scripts.live_release_validation.cleanup import workloads as cleanup_workloads
from scripts.live_release_validation.inventory import _shared as inventory_shared
from scripts.live_release_validation.inventory import ecr as inventory_ecr
from scripts.live_release_validation.inventory import project as inventory_project
from scripts.live_release_validation.inventory import scanners as inventory_scanners
from scripts.live_release_validation.inventory import stacks as inventory_stacks
from tests._live_validation_patching import patch_live_validation_helper
from tests.test_live_release_validation import _central_job, _context, _response

_REGION = "us-east-1"
_ACCOUNT = "123456789012"
_PROJECT = "gco-live"
_PUSHED_AT = datetime(2026, 7, 17, 12, 0, tzinfo=UTC)


def _client_error(code: str, message: str = "boom", operation: str = "Operation") -> ClientError:
    return ClientError({"Error": {"Code": code, "Message": message}}, operation)


def _paginated_client(pages_by_operation: Mapping[str, Any]) -> MagicMock:
    """Return a client whose ``get_paginator(op).paginate(...)`` yields fixed pages.

    A value may be a list of pages, or a callable receiving ``paginate``'s
    keyword arguments and returning the pages for that request.
    """
    client = MagicMock()
    paginators: dict[str, MagicMock] = {}
    for operation, pages in pages_by_operation.items():
        paginator = MagicMock(name=f"paginator-{operation}")
        if callable(pages):
            paginator.paginate.side_effect = pages
        else:
            paginator.paginate.return_value = pages
        paginators[operation] = paginator
    client.get_paginator.side_effect = paginators.__getitem__
    return client


def _session_for(client: MagicMock, *services: str) -> MagicMock:
    """Return a session that hands out ``client`` and rejects unexpected services."""
    expected = set(services)

    def make_client(service: str, **kwargs: Any) -> MagicMock:
        if expected and service not in expected:
            raise AssertionError(f"Unexpected service client: {service}")
        return client

    session = MagicMock()
    session.client.side_effect = make_client
    return session


def _tags(**values: str) -> list[dict[str, str]]:
    return [{"Key": key, "Value": value} for key, value in values.items()]


@contextmanager
def _patched_helpers(fakes: Mapping[str, Any]):
    """Install several live-validation helper replacements at once."""
    with ExitStack() as stack:
        yield {
            name: stack.enter_context(patch_live_validation_helper(name, fake))
            for name, fake in fakes.items()
        }


# ---------------------------------------------------------------------------
# inventory/_shared.py
# ---------------------------------------------------------------------------


class TestSharedOwnershipPrimitives:
    def test_normalize_json_text_leaves_non_json_and_non_strings_alone(self) -> None:
        assert inventory_shared._normalize_json_text('{"a": 1}') == {"a": 1}
        assert inventory_shared._normalize_json_text("not json at all") == "not json at all"
        assert inventory_shared._normalize_json_text({"already": "parsed"}) == {"already": "parsed"}
        assert inventory_shared._normalize_json_text(None) is None

    def test_mapping_tags_accepts_only_mappings(self) -> None:
        assert inventory_shared._mapping_tags(None) == {}
        assert inventory_shared._mapping_tags({"gco:project": _PROJECT, 1: 2}) == {
            "gco:project": _PROJECT,
            "1": "2",
        }
        with pytest.raises(RuntimeError, match="unexpected format"):
            inventory_shared._mapping_tags([{"Key": "gco:project", "Value": _PROJECT}])

    @pytest.mark.parametrize(
        ("arn", "expected"),
        [
            ("not-an-arn", False),
            ("arn:aws:ecr:us-east-1:123456789012", False),
            (f"arn:aws:ecr:{_REGION}:{_ACCOUNT}:repository/{_PROJECT}/api", True),
            (f"arn:aws:sqs:{_REGION}:{_ACCOUNT}:{_PROJECT}-jobs", True),
            (f"arn:aws:sqs:{_REGION}:{_ACCOUNT}:{_PROJECT}", True),
            (f"arn:aws:s3:::{_PROJECT}", True),
            (f"arn:aws:lambda:{_REGION}:{_ACCOUNT}:function:{_PROJECT}-worker", True),
            (f"arn:aws:iam::{_ACCOUNT}:role/{_PROJECT}/nested/name", True),
            (f"arn:aws:ecr:{_REGION}:{_ACCOUNT}:repository/{_PROJECT}ish/api", False),
            (f"arn:aws:s3:::{_PROJECT}ish", False),
            # The leading component is the resource *type*, never an owned name.
            (f"arn:aws:ecr:{_REGION}:{_ACCOUNT}:{_PROJECT}/other", False),
        ],
    )
    def test_arn_ownership_requires_an_exact_project_component(
        self, arn: str, expected: bool
    ) -> None:
        assert inventory_shared._arn_is_project_owned(arn, _PROJECT) is expected

    def test_iam_ownership_accepts_name_path_or_tags_only(self) -> None:
        owned = inventory_shared._iam_resource_is_project_owned
        assert owned(f"{_PROJECT}-role", "/", {}, _PROJECT) is True
        assert owned("role", f"/{_PROJECT}/", {}, _PROJECT) is True
        assert owned("role", "/", {"gco:project": _PROJECT}, _PROJECT) is True
        assert owned("role", "/", {"aws:cloudformation:stack-name": f"{_PROJECT}-x"}, _PROJECT)
        assert owned(f"{_PROJECT}ish", f"/{_PROJECT}ish/", {"gco:project": "other"}, _PROJECT) is (
            False
        )


# ---------------------------------------------------------------------------
# inventory/scanners.py
# ---------------------------------------------------------------------------


class TestEksSqsDynamoScanners:
    def test_eks_cluster_without_a_name_fails_closed(self) -> None:
        client = _paginated_client({"list_clusters": [{"clusters": ["gco-live", ""]}]})

        with pytest.raises(RuntimeError, match="cluster without a name"):
            inventory_scanners._list_eks_clusters(_session_for(client, "eks"), _REGION, None)

    def test_sqs_queues_are_matched_on_the_exact_queue_name(self) -> None:
        base = f"https://sqs.{_REGION}.amazonaws.com/{_ACCOUNT}"
        client = _paginated_client(
            {
                "list_queues": [
                    {"QueueUrls": [f"{base}/{_PROJECT}-jobs", f"{base}/{_PROJECT}ish-jobs"]},
                    {"QueueUrls": [f"{base}/{_PROJECT}", f"{base}/{_PROJECT}-jobs"]},
                    {},
                ]
            }
        )
        session = _session_for(client, "sqs")

        urls = inventory_scanners._list_sqs_queues(session, _REGION, _PROJECT)

        assert urls == [f"{base}/{_PROJECT}", f"{base}/{_PROJECT}-jobs"]
        session.client.assert_called_once_with("sqs", region_name=_REGION)
        client.get_paginator("list_queues").paginate.assert_called_once_with(
            QueueNamePrefix=_PROJECT
        )

    def test_dynamodb_tables_are_deduplicated_and_sorted(self) -> None:
        client = _paginated_client(
            {
                "list_tables": [
                    {"TableNames": [f"{_PROJECT}-jobs", "unrelated", _PROJECT]},
                    {"TableNames": [f"{_PROJECT}-jobs", f"{_PROJECT}ish"]},
                ]
            }
        )

        tables = inventory_scanners._list_dynamodb_tables(
            _session_for(client, "dynamodb"), _REGION, _PROJECT
        )

        assert tables == [_PROJECT, f"{_PROJECT}-jobs"]


class TestLoadBalancerScanner:
    def test_ownership_signals_and_tag_batching(self) -> None:
        def arn(index: int) -> str:
            return (
                f"arn:aws:elasticloadbalancing:{_REGION}:{_ACCOUNT}:"
                f"loadbalancer/app/lb-{index:02d}/id"
            )

        load_balancers = [
            {"LoadBalancerArn": arn(index), "LoadBalancerName": f"k8s-shared-{index:02d}"}
            for index in range(21)
        ]
        load_balancers[0]["LoadBalancerName"] = f"{_PROJECT}-api"
        del load_balancers[3]["LoadBalancerName"]
        tags_by_arn = {
            arn(1): _tags(**{"aws:cloudformation:stack-name": f"{_PROJECT}-{_REGION}"}),
            arn(2): _tags(**{"gco:project": _PROJECT}),
            arn(4): _tags(**{"gco:project": "someone-else"}),
        }
        client = _paginated_client(
            {
                "describe_load_balancers": [
                    {"LoadBalancers": load_balancers[:10]},
                    {"LoadBalancers": load_balancers[10:]},
                ]
            }
        )
        client.describe_tags.side_effect = lambda *, ResourceArns: {
            "TagDescriptions": [
                {"ResourceArn": item, "Tags": tags_by_arn.get(item, [])} for item in ResourceArns
            ]
        }

        owned = inventory_scanners._list_load_balancers(
            _session_for(client, "elbv2"), _REGION, _PROJECT
        )

        assert owned == [arn(0), arn(1), arn(2)]
        assert [
            len(call.kwargs["ResourceArns"]) for call in client.describe_tags.call_args_list
        ] == [20, 1]


class TestInstanceScanner:
    def test_instance_without_an_id_fails_closed(self) -> None:
        client = _paginated_client(
            {"describe_instances": [{"Reservations": [{"Instances": [{"Tags": []}]}]}]}
        )

        with pytest.raises(RuntimeError, match="instance without an ID"):
            inventory_scanners._list_instance_inventory(
                _session_for(client, "ec2"), _REGION, _PROJECT
            )

    def test_list_instances_returns_only_the_project_owned_ids(self) -> None:
        client = _paginated_client(
            {
                "describe_instances": [
                    {
                        "Reservations": [
                            {
                                "Instances": [
                                    {"InstanceId": "i-0000000000000000b", "Tags": []},
                                    {
                                        "InstanceId": "i-0000000000000000a",
                                        "Tags": _tags(Name=f"{_PROJECT}-node"),
                                    },
                                ]
                            }
                        ]
                    }
                ]
            }
        )

        assert inventory_scanners._list_instances(
            _session_for(client, "ec2"), _REGION, _PROJECT
        ) == ["i-0000000000000000a"]


class TestKmsKeyScanner:
    _KEYS: dict[str, dict[str, Any]] = {
        "k-aws": {"KeyManager": "AWS", "KeyState": "Enabled"},
        "k-project": {"KeyManager": "CUSTOMER", "KeyState": "Enabled", "Description": "EKS"},
        "k-other-pending": {
            "KeyManager": "CUSTOMER",
            "KeyState": "PendingDeletion",
            "DeletionDate": _PUSHED_AT,
        },
        "k-other-active": {"KeyManager": "CUSTOMER", "KeyState": "Enabled"},
        "k-this-run": {
            "KeyManager": "CUSTOMER",
            "KeyState": "PendingDeletion",
            "DeletionDate": _PUSHED_AT,
        },
        "k-unrelated": {"KeyManager": "CUSTOMER", "KeyState": "Enabled"},
    }
    _TAGS: dict[str, dict[str, str]] = {
        "k-project": {"gco:project": _PROJECT},
        "k-other-pending": {constants._RUN_STACK_TAG: "run-999"},
        "k-other-active": {constants._RUN_STACK_TAG: "run-999"},
        "k-this-run": {constants._RUN_STACK_TAG: "run-123"},
        "k-unrelated": {"Name": "someone-elses-key"},
    }

    def _client(self) -> MagicMock:
        client = _paginated_client(
            {
                "list_keys": [
                    {"Keys": [{"KeyId": ""}, {"KeyId": "k-aws"}, {"KeyId": "k-project"}]},
                    {
                        "Keys": [
                            {"KeyId": "k-other-pending"},
                            {"KeyId": "k-other-active"},
                            {"KeyId": "k-this-run"},
                            {"KeyId": "k-unrelated"},
                        ]
                    },
                ]
            }
        )

        def describe_key(*, KeyId: str) -> dict[str, Any]:
            metadata = dict(self._KEYS[KeyId])
            metadata["Arn"] = f"arn:aws:kms:{_REGION}:{_ACCOUNT}:key/{KeyId}"
            return {"KeyMetadata": metadata}

        def list_resource_tags(*, KeyId: str, Marker: str | None = None) -> dict[str, Any]:
            tags = [{"TagKey": key, "TagValue": value} for key, value in self._TAGS[KeyId].items()]
            if KeyId == "k-project" and Marker is None:
                # First page is truncated; the second carries a tag without a key.
                return {"Tags": [], "Truncated": True, "NextMarker": "page-2"}
            if KeyId == "k-project":
                assert Marker == "page-2"
                return {"Tags": [*tags, {"TagValue": "orphan"}], "Truncated": False}
            return {"Tags": tags}

        client.describe_key.side_effect = describe_key
        client.list_resource_tags.side_effect = list_resource_tags
        return client

    @staticmethod
    def _ids(keys: list[dict[str, Any]]) -> list[str]:
        return [key["key_id"] for key in keys]

    def test_without_a_run_id_only_project_tagged_customer_keys_are_listed(self) -> None:
        client = self._client()

        keys = inventory_scanners._list_project_kms_keys(
            _session_for(client, "kms"), _REGION, _PROJECT
        )

        assert self._ids(keys) == ["k-project"]
        assert keys[0] == {
            "key_id": "k-project",
            "arn": f"arn:aws:kms:{_REGION}:{_ACCOUNT}:key/k-project",
            "state": "Enabled",
            "description": "EKS",
            "deletion_date": None,
            "tags": {"gco:project": _PROJECT},
        }
        # AWS-managed keys are never described further, and the nameless
        # summary is skipped before any describe call.
        described = [call.kwargs["KeyId"] for call in client.describe_key.call_args_list]
        assert "" not in described
        assert client.list_resource_tags.call_args_list[0].kwargs == {"KeyId": "k-project"}
        assert client.list_resource_tags.call_args_list[1].kwargs == {
            "KeyId": "k-project",
            "Marker": "page-2",
        }

    def test_with_a_run_id_only_other_runs_pending_deletion_are_isolated(self) -> None:
        client = self._client()

        keys = inventory_scanners._list_project_kms_keys(
            _session_for(client, "kms"), _REGION, _PROJECT, "run-123"
        )

        # Another run's *active* key still fails closed; its pending key does not.
        assert self._ids(keys) == ["k-other-active", "k-project", "k-this-run"]
        this_run = keys[-1]
        assert this_run["state"] == "PendingDeletion"
        assert this_run["deletion_date"] == _PUSHED_AT.isoformat()
        assert this_run["tags"] == {constants._RUN_STACK_TAG: "run-123"}


class TestEcrRepositoryAndGlobalAcceleratorScanners:
    def test_ecr_repositories_are_filtered_from_the_shared_inventory(self) -> None:
        session = MagicMock()
        inventory = {
            _REGION: [{"name": f"{_PROJECT}/api"}, {"name": "mirror/repo"}, {"name": _PROJECT}]
        }

        with patch_live_validation_helper("collect_ecr_inventory", return_value=inventory) as fake:
            names = inventory_scanners._list_project_ecr_repositories(session, _REGION, _PROJECT)

        assert names == [_PROJECT, f"{_PROJECT}/api"]
        fake.assert_called_once_with(session, [_REGION])

    def test_control_region_is_none_outside_supported_partitions(self) -> None:
        session = MagicMock()
        session.get_partition_for_region.return_value = "aws-cn"

        assert inventory_scanners._global_accelerator_control_region(session, "cn-north-1") is None
        session.get_available_regions.assert_not_called()

    def test_control_region_must_be_advertised_by_the_sdk(self) -> None:
        session = MagicMock()
        session.get_partition_for_region.return_value = "aws"
        session.get_available_regions.return_value = ["us-east-1"]

        with pytest.raises(RuntimeError, match="does not advertise"):
            inventory_scanners._global_accelerator_control_region(session, _REGION)

        session.get_available_regions.return_value = ["us-east-1", "us-west-2"]
        assert inventory_scanners._global_accelerator_control_region(session, _REGION) == (
            "us-west-2"
        )
        session.get_available_regions.assert_called_with("globalaccelerator", partition_name="aws")

    def test_accelerators_are_not_listed_without_a_control_region(self) -> None:
        session = MagicMock()

        assert inventory_scanners._list_global_accelerators(session, None, _PROJECT) == []
        session.client.assert_not_called()

    def test_accelerators_are_paginated_and_matched_by_name_or_tags(self) -> None:
        def arn(suffix: str) -> str:
            return f"arn:aws:globalaccelerator::{_ACCOUNT}:accelerator/{suffix}"

        client = MagicMock()
        client.list_accelerators.side_effect = [
            {
                "Accelerators": [
                    {"AcceleratorArn": arn("named"), "Name": f"{_PROJECT}-edge"},
                    {"AcceleratorArn": arn("tagged"), "Name": "edge"},
                ],
                "NextToken": "page-2",
            },
            {
                "Accelerators": [
                    {"AcceleratorArn": arn("unrelated"), "Name": "someone-elses"},
                    {"AcceleratorArn": arn("named"), "Name": f"{_PROJECT}-edge"},
                ]
            },
        ]
        client.list_tags_for_resource.side_effect = lambda *, ResourceArn: {
            "Tags": _tags(**{"gco:project": _PROJECT}) if ResourceArn == arn("tagged") else []
        }
        session = _session_for(client, "globalaccelerator")

        owned = inventory_scanners._list_global_accelerators(session, "us-west-2", _PROJECT)

        assert owned == [arn("named"), arn("tagged")]
        session.client.assert_called_once_with("globalaccelerator", region_name="us-west-2")
        assert [call.kwargs for call in client.list_accelerators.call_args_list] == [
            {},
            {"NextToken": "page-2"},
        ]


class TestTaggedResourceAndClusterVolumeScanners:
    def test_tagging_api_records_without_an_arn_fail_closed(self) -> None:
        client = _paginated_client(
            {"get_resources": [{"ResourceTagMappingList": [{"ResourceARN": "", "Tags": []}]}]}
        )

        with pytest.raises(RuntimeError, match="omitted an ARN"):
            inventory_scanners._list_project_tagged_resources(
                _session_for(client, "resourcegroupstaggingapi"), _REGION, _PROJECT
            )

    def test_tagging_api_matches_project_tags_or_arn_components_once(self) -> None:
        tagged = f"arn:aws:ssm:{_REGION}:{_ACCOUNT}:parameter/shared-name"
        by_arn = f"arn:aws:sqs:{_REGION}:{_ACCOUNT}:{_PROJECT}-jobs"
        unrelated = f"arn:aws:sqs:{_REGION}:{_ACCOUNT}:other-jobs"
        client = _paginated_client(
            {
                "get_resources": [
                    {
                        "ResourceTagMappingList": [
                            {"ResourceARN": tagged, "Tags": _tags(**{"gco:project": _PROJECT})},
                            {"ResourceARN": unrelated, "Tags": _tags(Name="other")},
                        ]
                    },
                    {
                        "ResourceTagMappingList": [
                            {"ResourceARN": by_arn, "Tags": []},
                            {"ResourceARN": tagged, "Tags": _tags(**{"gco:project": _PROJECT})},
                        ]
                    },
                ]
            }
        )

        resources = inventory_scanners._list_project_tagged_resources(
            _session_for(client, "resourcegroupstaggingapi"), _REGION, _PROJECT
        )

        assert resources == [
            {"arn": by_arn, "tags": {}},
            {"arn": tagged, "tags": {"gco:project": _PROJECT}},
        ]

    def test_cluster_volume_scanner_skips_unrelated_tags_and_empty_cluster_names(self) -> None:
        client = _paginated_client(
            {
                "describe_volumes": [
                    {
                        "Volumes": [
                            {
                                "VolumeId": "vol-00000000000000001",
                                "State": "available",
                                "Tags": _tags(
                                    **{
                                        "kubernetes.io/created-for/pvc/name": "data",
                                        f"kubernetes.io/cluster/{_PROJECT}-{_REGION}": "owned",
                                    }
                                ),
                            },
                            {
                                "VolumeId": "vol-00000000000000002",
                                "State": "available",
                                "Tags": _tags(**{"kubernetes.io/cluster/": "owned"}),
                            },
                        ]
                    }
                ]
            }
        )

        assert inventory_scanners._list_cluster_volumes(
            _session_for(client, "ec2"), _REGION, _PROJECT
        ) == ["vol-00000000000000001"]


class TestEc2NetworkingScanner:
    @staticmethod
    def _pages() -> dict[str, list[dict[str, Any]]]:
        return {
            "describe_vpcs": [
                {
                    "Vpcs": [
                        {"VpcId": "vpc-11111111111111111", "Tags": _tags(Name=_PROJECT)},
                        {"VpcId": "vpc-other", "Tags": _tags(Name="someone-elses")},
                    ]
                }
            ],
            "describe_subnets": [
                {"Subnets": [{"SubnetId": "subnet-11111111111111111", "VpcId": "vpc-other"}]}
            ],
            "describe_nat_gateways": [
                {
                    "NatGateways": [
                        {
                            "NatGatewayId": "nat-11111111111111111",
                            "SubnetId": "subnet-11111111111111111",
                            "State": "available",
                        }
                    ]
                }
            ],
            "describe_security_groups": [
                {
                    "SecurityGroups": [
                        {
                            "GroupId": "sg-11111111111111111",
                            "GroupName": f"{_PROJECT}-nodes",
                            "VpcId": "vpc-other",
                        }
                    ]
                }
            ],
            "describe_network_interfaces": [
                {
                    "NetworkInterfaces": [
                        {
                            "NetworkInterfaceId": "eni-11111111111111111",
                            "VpcId": "vpc-other",
                            "SubnetId": "subnet-other",
                            "Groups": [{"GroupId": "sg-11111111111111111"}],
                        },
                        {
                            "NetworkInterfaceId": "eni-22222222222222222",
                            "VpcId": "vpc-other",
                            "SubnetId": "subnet-other",
                            "Groups": [{"GroupId": "sg-other"}],
                            "Attachment": {"InstanceId": "i-11111111111111111"},
                        },
                    ]
                }
            ],
            "describe_flow_logs": [
                {
                    "FlowLogs": [
                        {"FlowLogId": "fl-11111111111111111", "ResourceId": "i-11111111111111111"},
                        {"FlowLogId": "fl-22222222222222222", "ResourceId": "vpc-other"},
                    ]
                }
            ],
        }

    @staticmethod
    def _addresses() -> list[dict[str, Any]]:
        return [
            {"AllocationId": "eipalloc-11111111111111111", "InstanceId": "i-11111111111111111"},
            {"PublicIp": "203.0.113.7", "InstanceId": "i-other"},
        ]

    def _run(
        self,
        pages: dict[str, list[dict[str, Any]]],
        addresses: list[dict[str, Any]],
    ) -> tuple[dict[str, list[str]], dict[str, list[str]]]:
        client = _paginated_client(pages)
        client.describe_addresses.return_value = {"Addresses": addresses}
        return inventory_scanners._list_project_ec2_networking(
            _session_for(client, "ec2"), _REGION, _PROJECT, ["i-11111111111111111"]
        )

    def test_association_signals_claim_dependent_networking(self) -> None:
        project, authority = self._run(self._pages(), self._addresses())

        assert project == {
            "vpcs": ["vpc-11111111111111111"],
            # Subnet: neither tagged nor in an owned VPC.
            "subnets": [],
            # NAT gateway: only via a subnet that is not owned, so not claimed.
            "nat_gateways": [],
            # Flow log on the project instance is claimed; the one on a foreign VPC is not.
            "flow_logs": ["fl-11111111111111111"],
            # ENI via owned security group, ENI via attachment to a project instance.
            "network_interfaces": ["eni-11111111111111111", "eni-22222222222222222"],
            "security_groups": ["sg-11111111111111111"],
            "elastic_ips": ["eipalloc-11111111111111111"],
        }
        assert authority["elastic_ips"] == ["203.0.113.7", "eipalloc-11111111111111111"]
        assert authority["subnets"] == ["subnet-11111111111111111"]
        assert authority["nat_gateways"] == ["nat-11111111111111111"]

    @pytest.mark.parametrize(
        ("operation", "collection", "field", "message"),
        [
            ("describe_vpcs", "Vpcs", "VpcId", "VPC without an ID"),
            ("describe_subnets", "Subnets", "SubnetId", "subnet without an ID"),
            ("describe_nat_gateways", "NatGateways", "NatGatewayId", "NAT gateway without an ID"),
            (
                "describe_security_groups",
                "SecurityGroups",
                "GroupId",
                "security group without an ID",
            ),
            (
                "describe_network_interfaces",
                "NetworkInterfaces",
                "NetworkInterfaceId",
                "network interface without an ID",
            ),
            ("describe_flow_logs", "FlowLogs", "FlowLogId", "flow log without an ID"),
        ],
    )
    def test_every_networking_record_must_carry_an_id(
        self, operation: str, collection: str, field: str, message: str
    ) -> None:
        pages = self._pages()
        del pages[operation][0][collection][0][field]

        with pytest.raises(RuntimeError, match=message):
            self._run(pages, self._addresses())

    def test_elastic_ip_without_any_identity_fails_closed(self) -> None:
        with pytest.raises(RuntimeError, match="Elastic IP without an identity"):
            self._run(self._pages(), [{"InstanceId": "i-11111111111111111"}])


class TestLambdaApiGatewayLogSecretScanners:
    def test_lambda_functions_require_identity_and_match_name_or_tags(self) -> None:
        def arn(name: str) -> str:
            return f"arn:aws:lambda:{_REGION}:{_ACCOUNT}:function:{name}"

        client = _paginated_client(
            {
                "list_functions": [
                    {
                        "Functions": [
                            {"FunctionName": f"{_PROJECT}-worker", "FunctionArn": arn("worker")},
                            {"FunctionName": "tagged", "FunctionArn": arn("tagged")},
                            {"FunctionName": "unrelated", "FunctionArn": arn("unrelated")},
                        ]
                    }
                ]
            }
        )
        client.list_tags.side_effect = lambda *, Resource: {
            "Tags": {"gco:project": _PROJECT} if Resource == arn("tagged") else {}
        }

        assert inventory_scanners._list_lambda_functions(
            _session_for(client, "lambda"), _REGION, _PROJECT
        ) == [arn("tagged"), arn("worker")]

        client = _paginated_client(
            {"list_functions": [{"Functions": [{"FunctionName": "no-arn", "FunctionArn": ""}]}]}
        )
        with pytest.raises(RuntimeError, match="function without identity"):
            inventory_scanners._list_lambda_functions(
                _session_for(client, "lambda"), _REGION, _PROJECT
            )

    @pytest.mark.parametrize(
        ("scanner", "operation", "items_key", "id_key", "name_key", "tags_key", "label"),
        [
            (
                inventory_scanners._list_api_gateway_v1_apis,
                "get_rest_apis",
                "items",
                "id",
                "name",
                "tags",
                "API Gateway v1",
            ),
            (
                inventory_scanners._list_api_gateway_v2_apis,
                "get_apis",
                "Items",
                "ApiId",
                "Name",
                "Tags",
                "API Gateway v2",
            ),
        ],
    )
    def test_api_gateway_scanners_require_ids_only_for_owned_apis(
        self,
        scanner: Callable[..., list[str]],
        operation: str,
        items_key: str,
        id_key: str,
        name_key: str,
        tags_key: str,
        label: str,
    ) -> None:
        client = _paginated_client(
            {
                operation: [
                    {
                        items_key: [
                            {id_key: "named", name_key: f"{_PROJECT}-api"},
                            {
                                id_key: "tagged",
                                name_key: "api",
                                tags_key: {"gco:project": _PROJECT},
                            },
                            {name_key: "unrelated-without-id"},
                            {id_key: "unrelated", name_key: "other", tags_key: None},
                        ]
                    }
                ]
            }
        )

        assert scanner(_session_for(client), _REGION, _PROJECT) == ["named", "tagged"]

        client = _paginated_client({operation: [{items_key: [{name_key: f"{_PROJECT}-api"}]}]})
        with pytest.raises(RuntimeError, match=f"{label} returned an API without an ID"):
            scanner(_session_for(client), _REGION, _PROJECT)

    def test_log_groups_match_path_components_or_tags_and_strip_arn_wildcards(self) -> None:
        def arn(name: str) -> str:
            return f"arn:aws:logs:{_REGION}:{_ACCOUNT}:log-group:{name}"

        client = _paginated_client(
            {
                "describe_log_groups": [
                    {
                        "logGroups": [
                            {
                                "logGroupName": f"/aws/lambda/{_PROJECT}-worker",
                                "logGroupArn": arn(f"/aws/lambda/{_PROJECT}-worker"),
                            },
                            {"logGroupName": "/tagged", "arn": arn("/tagged") + ":*"},
                            {"logGroupName": "/unrelated", "arn": arn("/unrelated") + ":*"},
                        ]
                    }
                ]
            }
        )
        client.list_tags_for_resource.side_effect = lambda *, resourceArn: {
            "tags": {"gco:project": _PROJECT} if resourceArn == arn("/tagged") else None
        }

        names = inventory_scanners._list_cloudwatch_log_groups(
            _session_for(client, "logs"), _REGION, _PROJECT
        )

        assert names == [f"/aws/lambda/{_PROJECT}-worker", "/tagged"]
        assert [
            call.kwargs["resourceArn"] for call in client.list_tags_for_resource.call_args_list
        ][1] == arn("/tagged")

        client = _paginated_client({"describe_log_groups": [{"logGroups": [{"arn": arn("/x")}]}]})
        with pytest.raises(RuntimeError, match="log group without identity"):
            inventory_scanners._list_cloudwatch_log_groups(
                _session_for(client, "logs"), _REGION, _PROJECT
            )

    def test_secrets_require_an_arn_only_when_owned(self) -> None:
        def arn(name: str) -> str:
            return f"arn:aws:secretsmanager:{_REGION}:{_ACCOUNT}:secret:{name}-AbCdEf"

        client = _paginated_client(
            {
                "list_secrets": [
                    {
                        "SecretList": [
                            {"Name": f"{_PROJECT}/db", "ARN": arn("db")},
                            {
                                "Name": "tagged",
                                "ARN": arn("tagged"),
                                "Tags": _tags(**{"gco:project": _PROJECT}),
                            },
                            {"Name": "unrelated-without-arn"},
                        ]
                    }
                ]
            }
        )

        assert inventory_scanners._list_secrets(
            _session_for(client, "secretsmanager"), _REGION, _PROJECT
        ) == [arn("db"), arn("tagged")]
        client.get_paginator("list_secrets").paginate.assert_called_once_with(
            IncludePlannedDeletion=True
        )

        client = _paginated_client({"list_secrets": [{"SecretList": [{"Name": f"{_PROJECT}/db"}]}]})
        with pytest.raises(RuntimeError, match="project-owned secret without an ARN"):
            inventory_scanners._list_secrets(
                _session_for(client, "secretsmanager"), _REGION, _PROJECT
            )


class TestS3AndIamScanners:
    @pytest.mark.parametrize("code", ["NoSuchTagSet", "NoSuchTagSetError"])
    def test_bucket_without_a_tag_set_has_no_tags(self, code: str) -> None:
        client = MagicMock()
        client.get_bucket_tagging.side_effect = _client_error(code)

        assert inventory_scanners._list_s3_bucket_tags(client, "bucket") == {}

    def test_other_bucket_tagging_errors_propagate(self) -> None:
        client = MagicMock()
        client.get_bucket_tagging.side_effect = _client_error("AccessDenied")

        with pytest.raises(ClientError):
            inventory_scanners._list_s3_bucket_tags(client, "bucket")

    def test_buckets_match_name_or_tags_and_require_a_name(self) -> None:
        client = MagicMock()
        client.list_buckets.return_value = {
            "Buckets": [{"Name": "tagged"}, {"Name": f"{_PROJECT}-assets"}, {"Name": "unrelated"}]
        }
        client.get_bucket_tagging.side_effect = lambda *, Bucket: (
            {"TagSet": _tags(**{"gco:project": _PROJECT})} if Bucket == "tagged" else {"TagSet": []}
        )
        session = _session_for(client, "s3")

        assert inventory_scanners._list_project_s3_buckets(session, _REGION, _PROJECT) == [
            f"{_PROJECT}-assets",
            "tagged",
        ]
        session.client.assert_called_once_with("s3", region_name=_REGION)

        client.list_buckets.return_value = {"Buckets": [{}]}
        with pytest.raises(RuntimeError, match="bucket without a name"):
            inventory_scanners._list_project_s3_buckets(session, _REGION, _PROJECT)

    def test_iam_tags_follow_markers_and_reject_truncation_without_one(self) -> None:
        client = MagicMock()
        client.list_role_tags.side_effect = [
            {"Tags": _tags(a="1"), "IsTruncated": True, "Marker": "m1"},
            {"Tags": _tags(b="2"), "IsTruncated": False},
        ]

        tags = inventory_scanners._list_iam_tags(client, "list_role_tags", "RoleName", "role")

        assert tags == {"a": "1", "b": "2"}
        assert [call.kwargs for call in client.list_role_tags.call_args_list] == [
            {"RoleName": "role"},
            {"RoleName": "role", "Marker": "m1"},
        ]

        client.list_role_tags.side_effect = [{"Tags": [], "IsTruncated": True}]
        with pytest.raises(RuntimeError, match="truncated its response without a Marker"):
            inventory_scanners._list_iam_tags(client, "list_role_tags", "RoleName", "role")

    @staticmethod
    def _iam_client() -> MagicMock:
        def arn(kind: str, name: str) -> str:
            return f"arn:aws:iam::{_ACCOUNT}:{kind}/{name}"

        client = _paginated_client(
            {
                "list_roles": [
                    {
                        "Roles": [
                            {
                                "RoleName": f"{_PROJECT}-role",
                                "Arn": arn("role", "named"),
                                "Path": "/",
                            },
                            {"RoleName": "shared", "Arn": arn("role", "shared"), "Path": "/"},
                        ]
                    }
                ],
                "list_policies": [
                    {
                        "Policies": [
                            {
                                "PolicyName": "policy",
                                "Arn": arn("policy", f"{_PROJECT}/policy"),
                                "Path": f"/{_PROJECT}/",
                            },
                            {"PolicyName": "shared", "Arn": arn("policy", "shared"), "Path": "/"},
                        ]
                    }
                ],
                "list_instance_profiles": [
                    {
                        "InstanceProfiles": [
                            {
                                "InstanceProfileName": "profile",
                                "Arn": arn("instance-profile", "profile"),
                                "Path": "/",
                            },
                            {
                                "InstanceProfileName": "shared",
                                "Arn": arn("instance-profile", "shared"),
                                "Path": "/",
                            },
                        ]
                    }
                ],
                "list_users": [
                    {
                        "Users": [
                            {"UserName": "someone", "Arn": arn("user", "someone"), "Path": "/"},
                            {"UserName": "deployer", "Arn": arn("user", "deployer"), "Path": "/"},
                        ]
                    }
                ],
                "list_groups": [
                    {
                        "Groups": [
                            {
                                "GroupName": "ops",
                                "Arn": arn("group", "ops"),
                                "Path": f"/{_PROJECT}/",
                            },
                            {"GroupName": "admins", "Arn": arn("group", "admins")},
                        ]
                    }
                ],
            }
        )
        client.list_role_tags.return_value = {"Tags": []}
        client.list_policy_tags.return_value = {"Tags": []}
        client.list_instance_profile_tags.side_effect = lambda *, InstanceProfileName: {
            "Tags": (
                _tags(**{"aws:cloudformation:stack-name": f"{_PROJECT}-{_REGION}"})
                if InstanceProfileName == "profile"
                else []
            )
        }
        client.list_user_tags.side_effect = lambda *, UserName: {
            "Tags": _tags(**{"gco:project": _PROJECT}) if UserName == "deployer" else []
        }
        return client

    def test_iam_resources_match_name_path_or_tags_per_kind(self) -> None:
        client = self._iam_client()

        resources = inventory_scanners._list_project_iam_resources(
            _session_for(client, "iam"), _REGION, _PROJECT
        )

        assert resources == {
            "iam_roles": [f"arn:aws:iam::{_ACCOUNT}:role/named"],
            "iam_policies": [f"arn:aws:iam::{_ACCOUNT}:policy/{_PROJECT}/policy"],
            "iam_instance_profiles": [f"arn:aws:iam::{_ACCOUNT}:instance-profile/profile"],
            "iam_users": [f"arn:aws:iam::{_ACCOUNT}:user/deployer"],
            "iam_groups": [f"arn:aws:iam::{_ACCOUNT}:group/ops"],
        }
        client.get_paginator("list_policies").paginate.assert_called_once_with(Scope="Local")
        assert [call.kwargs["PolicyArn"] for call in client.list_policy_tags.call_args_list] == [
            f"arn:aws:iam::{_ACCOUNT}:policy/{_PROJECT}/policy",
            f"arn:aws:iam::{_ACCOUNT}:policy/shared",
        ]
        assert [
            call.kwargs["InstanceProfileName"]
            for call in client.list_instance_profile_tags.call_args_list
        ] == ["profile", "shared"]
        assert [call.kwargs["UserName"] for call in client.list_user_tags.call_args_list] == [
            "someone",
            "deployer",
        ]

    @pytest.mark.parametrize(
        ("operation", "collection", "message"),
        [
            ("list_roles", "Roles", "role without identity"),
            ("list_policies", "Policies", "customer-managed policy without identity"),
            ("list_instance_profiles", "InstanceProfiles", "instance profile without identity"),
            ("list_users", "Users", "user without identity"),
            ("list_groups", "Groups", "group without identity"),
        ],
    )
    def test_every_iam_record_must_carry_a_name_and_arn(
        self, operation: str, collection: str, message: str
    ) -> None:
        client = self._iam_client()
        client.get_paginator(operation).paginate.return_value = [{collection: [{"Path": "/"}]}]

        with pytest.raises(RuntimeError, match=message):
            inventory_scanners._list_project_iam_resources(
                _session_for(client, "iam"), _REGION, _PROJECT
            )


class TestBackupScanner:
    _VAULT_ARN = f"arn:aws:backup:{_REGION}:{_ACCOUNT}:backup-vault:{_PROJECT}-vault"
    _SHARED_VAULT_ARN = f"arn:aws:backup:{_REGION}:{_ACCOUNT}:backup-vault:shared"
    _PLAN_ARN = f"arn:aws:backup:{_REGION}:{_ACCOUNT}:backup-plan:plan-owned"
    _SHARED_PLAN_ARN = f"arn:aws:backup:{_REGION}:{_ACCOUNT}:backup-plan:plan-shared"

    @classmethod
    def _recovery_point(cls, suffix: str) -> str:
        return f"arn:aws:ec2:{_REGION}::snapshot/snap-{suffix}"

    def _client(self) -> MagicMock:
        recovery_points = {
            f"{_PROJECT}-vault": [
                {"RecoveryPoints": [{"RecoveryPointArn": self._recovery_point("in-owned-vault")}]}
            ],
            "shared": [
                {
                    "RecoveryPoints": [
                        {
                            "RecoveryPointArn": self._recovery_point("by-name"),
                            "ResourceName": f"{_PROJECT}-volume",
                        },
                        {
                            "RecoveryPointArn": self._recovery_point("by-arn"),
                            "ResourceArn": f"arn:aws:dynamodb:{_REGION}:{_ACCOUNT}:table/{_PROJECT}-jobs",
                        },
                        {"RecoveryPointArn": self._recovery_point("by-tags")},
                        {"RecoveryPointArn": self._recovery_point("foreign"), "ResourceName": "x"},
                    ]
                }
            ],
        }
        selections = {
            "plan-owned": [
                {"BackupSelectionsList": [{"SelectionId": "sel-1", "SelectionName": "anything"}]}
            ],
            "plan-shared": [
                {
                    "BackupSelectionsList": [
                        {"SelectionId": "sel-2", "SelectionName": f"{_PROJECT}-selection"},
                        {"SelectionId": "sel-3", "SelectionName": "foreign"},
                    ]
                }
            ],
        }
        client = _paginated_client(
            {
                "list_backup_vaults": [
                    {
                        "BackupVaultList": [
                            {
                                "BackupVaultName": f"{_PROJECT}-vault",
                                "BackupVaultArn": self._VAULT_ARN,
                            },
                            {"BackupVaultName": "shared", "BackupVaultArn": self._SHARED_VAULT_ARN},
                        ]
                    }
                ],
                "list_recovery_points_by_backup_vault": lambda **kwargs: recovery_points[
                    kwargs["BackupVaultName"]
                ],
                "list_backup_plans": [
                    {
                        "BackupPlansList": [
                            {
                                "BackupPlanId": "plan-owned",
                                "BackupPlanName": "plan",
                                "BackupPlanArn": self._PLAN_ARN,
                            },
                            {
                                "BackupPlanId": "plan-shared",
                                "BackupPlanName": "shared",
                                "BackupPlanArn": self._SHARED_PLAN_ARN,
                            },
                        ]
                    }
                ],
                "list_backup_selections": lambda **kwargs: selections[kwargs["BackupPlanId"]],
            }
        )
        project_tagged = {self._PLAN_ARN, self._recovery_point("by-tags")}
        client.list_tags.side_effect = lambda *, ResourceArn: {
            "Tags": {"gco:project": _PROJECT} if ResourceArn in project_tagged else None
        }
        return client

    def test_backup_resources_are_claimed_only_on_explicit_signals(self) -> None:
        resources = inventory_scanners._list_project_backup_resources(
            _session_for(self._client(), "backup"), _REGION, _PROJECT
        )

        assert resources == {
            "backup_vaults": [self._VAULT_ARN],
            "backup_plans": [self._PLAN_ARN],
            "backup_selections": ["plan-owned:sel-1", "plan-shared:sel-2"],
            "backup_recovery_points": sorted(
                [
                    self._recovery_point("in-owned-vault"),
                    self._recovery_point("by-name"),
                    self._recovery_point("by-arn"),
                    self._recovery_point("by-tags"),
                ]
            ),
        }

    @pytest.mark.parametrize(
        ("operation", "collection", "record", "message"),
        [
            (
                "list_backup_vaults",
                "BackupVaultList",
                {"BackupVaultName": "v"},
                "vault without identity",
            ),
            (
                "list_recovery_points_by_backup_vault",
                "RecoveryPoints",
                {"ResourceName": "x"},
                "recovery point without an ARN",
            ),
            (
                "list_backup_plans",
                "BackupPlansList",
                {"BackupPlanId": "p"},
                "plan without identity",
            ),
            (
                "list_backup_selections",
                "BackupSelectionsList",
                {"SelectionName": "s"},
                "selection without an ID",
            ),
        ],
    )
    def test_every_backup_record_must_carry_its_identity(
        self, operation: str, collection: str, record: dict[str, str], message: str
    ) -> None:
        client = self._client()
        paginator = client.get_paginator(operation)
        paginator.paginate.side_effect = None
        paginator.paginate.return_value = [{collection: [record]}]

        with pytest.raises(RuntimeError, match=message):
            inventory_scanners._list_project_backup_resources(
                _session_for(client, "backup"), _REGION, _PROJECT
            )


# ---------------------------------------------------------------------------
# inventory/stacks.py
# ---------------------------------------------------------------------------


class TestStackInventory:
    _STACK_ID = f"arn:aws:cloudformation:{_REGION}:{_ACCOUNT}:stack/{_PROJECT}-global/uuid"

    @staticmethod
    def _does_not_exist() -> ClientError:
        return _client_error(
            "ValidationError", f"Stack with id {_PROJECT}-global does not exist", "DescribeStacks"
        )

    def test_enabled_regions_intersect_opted_in_regions_with_cloudformation_support(self) -> None:
        ec2 = MagicMock()
        ec2.describe_regions.return_value = {
            "Regions": [
                {"RegionName": "us-east-1", "OptInStatus": "opt-in-not-required"},
                {"RegionName": "us-west-2"},
                {"RegionName": "eu-west-1", "OptInStatus": "opted-in"},
                {"RegionName": "ap-east-1", "OptInStatus": "not-opted-in"},
                {"RegionName": "sa-east-1", "OptInStatus": "opted-in"},
                {"RegionName": ""},
            ]
        }
        session = _session_for(ec2, "ec2")
        session.get_partition_for_region.return_value = "aws"
        session.get_available_regions.return_value = ["us-west-2", "us-east-1", "eu-west-1"]

        regions = inventory_stacks.discover_enabled_regions(session, _REGION)

        assert regions == ["eu-west-1", "us-east-1", "us-west-2"]
        ec2.describe_regions.assert_called_once_with(AllRegions=False)
        session.get_available_regions.assert_called_once_with(
            "cloudformation", partition_name="aws"
        )

    def test_region_discovery_fails_without_a_partition_or_any_region(self) -> None:
        session = MagicMock()
        session.get_partition_for_region.return_value = None
        with pytest.raises(RuntimeError, match="Could not resolve AWS partition"):
            inventory_stacks.discover_enabled_regions(session, _REGION)

        session.get_partition_for_region.return_value = "aws"
        session.get_available_regions.return_value = ["us-east-1"]
        session.client.return_value.describe_regions.return_value = {
            "Regions": [{"RegionName": "ap-east-1", "OptInStatus": "not-opted-in"}]
        }
        with pytest.raises(RuntimeError, match="No enabled CloudFormation Regions"):
            inventory_stacks.discover_enabled_regions(session, _REGION)

    def test_active_stacks_exclude_deleted_and_nameless_summaries(self) -> None:
        client = _paginated_client(
            {
                "list_stacks": [
                    {
                        "StackSummaries": [
                            {"StackName": "zeta", "StackId": "z", "StackStatus": "CREATE_COMPLETE"},
                            {"StackName": "gone", "StackId": "g", "StackStatus": "DELETE_COMPLETE"},
                            {"StackId": "anonymous", "StackStatus": "CREATE_COMPLETE"},
                        ]
                    },
                    {
                        "StackSummaries": [
                            {
                                "StackName": "alpha",
                                "StackId": "a2",
                                "StackStatus": "UPDATE_COMPLETE",
                            },
                            {"StackName": "alpha", "StackId": "a1", "StackStatus": "DELETE_FAILED"},
                        ]
                    },
                ]
            }
        )

        stacks = inventory_stacks.list_active_stacks(
            _session_for(client, "cloudformation"), _REGION
        )

        assert stacks == [
            {"name": "alpha", "stack_id": "a1", "status": "DELETE_FAILED"},
            {"name": "alpha", "stack_id": "a2", "status": "UPDATE_COMPLETE"},
            {"name": "zeta", "stack_id": "z", "status": "CREATE_COMPLETE"},
        ]

    def test_describe_stack_returns_none_only_for_authoritative_nonexistence(self) -> None:
        client = MagicMock()
        session = _session_for(client, "cloudformation")

        client.describe_stacks.side_effect = self._does_not_exist()
        assert inventory_stacks.describe_stack(session, _REGION, f"{_PROJECT}-global") is None

        client.describe_stacks.side_effect = _client_error("ValidationError", "Rate exceeded")
        with pytest.raises(ClientError):
            inventory_stacks.describe_stack(session, _REGION, f"{_PROJECT}-global")

        client.describe_stacks.side_effect = _client_error("AccessDenied", "does not exist")
        with pytest.raises(ClientError):
            inventory_stacks.describe_stack(session, _REGION, f"{_PROJECT}-global")

        client.describe_stacks.side_effect = None
        client.describe_stacks.return_value = {"Stacks": []}
        assert inventory_stacks.describe_stack(session, _REGION, f"{_PROJECT}-global") is None

    def test_describe_stack_normalizes_parameters_outputs_and_tags(self) -> None:
        client = MagicMock()
        client.describe_stacks.return_value = {
            "Stacks": [
                {
                    "StackId": self._STACK_ID,
                    "StackStatus": "CREATE_COMPLETE",
                    "Parameters": [
                        {"ParameterKey": "Zeta", "ParameterValue": "z", "ResolvedValue": "rz"},
                        {"ParameterKey": "Alpha"},
                        {"ParameterValue": "orphaned"},
                    ],
                    "Outputs": [
                        {"OutputKey": "ApiUrl", "OutputValue": "https://example.test"},
                        {"OutputKey": "Missing"},
                        {"OutputValue": "orphaned"},
                    ],
                    "Tags": _tags(**{"gco:project": _PROJECT}),
                    "EnableTerminationProtection": True,
                }
            ]
        }

        stack = inventory_stacks.describe_stack(
            _session_for(client, "cloudformation"), _REGION, f"{_PROJECT}-global"
        )

        assert stack == {
            "name": f"{_PROJECT}-global",
            "stack_id": self._STACK_ID,
            "status": "CREATE_COMPLETE",
            "parameters": [
                {"key": "Alpha", "value": "", "resolved_value": ""},
                {"key": "Zeta", "value": "z", "resolved_value": "rz"},
            ],
            "outputs": {"ApiUrl": "https://example.test"},
            "tags": {"gco:project": _PROJECT},
            "termination_protection": True,
        }

    def test_stack_resource_identities_must_be_complete_and_unique(self) -> None:
        client = _paginated_client(
            {
                "list_stack_resources": [
                    {
                        "StackResourceSummaries": [
                            {"LogicalResourceId": "Role", "ResourceType": "AWS::IAM::Role"}
                        ]
                    }
                ]
            }
        )
        with pytest.raises(RuntimeError, match="omitted a protected stack resource identity"):
            inventory_stacks._list_stack_resource_identities(client, self._STACK_ID)

        duplicate = {
            "LogicalResourceId": "Role",
            "ResourceType": "AWS::IAM::Role",
            "PhysicalResourceId": "role-name",
        }
        client = _paginated_client(
            {"list_stack_resources": [{"StackResourceSummaries": [duplicate, dict(duplicate)]}]}
        )
        with pytest.raises(RuntimeError, match="duplicated protected stack resource"):
            inventory_stacks._list_stack_resource_identities(client, self._STACK_ID)

    def _fingerprint_client(self, template_body: Any) -> MagicMock:
        client = _paginated_client({"list_stack_resources": [{"StackResourceSummaries": []}]})
        client.describe_stacks.return_value = {
            "Stacks": [
                {
                    "StackName": f"{_PROJECT}-global",
                    "StackId": self._STACK_ID,
                    "StackStatus": "CREATE_COMPLETE",
                }
            ]
        }
        client.get_template.return_value = {"TemplateBody": template_body}
        client.get_stack_policy.return_value = {"StackPolicyBody": '{"Statement": []}'}
        return client

    def test_fingerprint_is_none_for_an_absent_stack(self) -> None:
        client = MagicMock()
        client.describe_stacks.side_effect = self._does_not_exist()

        assert (
            inventory_stacks.describe_stack_fingerprint(
                _session_for(client, "cloudformation"), _REGION, f"{_PROJECT}-global"
            )
            is None
        )
        client.get_template.assert_not_called()

    def test_fingerprint_hashes_canonical_json_or_raw_text_templates(self) -> None:
        import hashlib

        json_client = self._fingerprint_client({"Resources": {"B": 1, "A": 2}})
        yaml_client = self._fingerprint_client("Resources:\n  A: {}\n")

        json_fingerprint = inventory_stacks.describe_stack_fingerprint(
            _session_for(json_client, "cloudformation"), _REGION, f"{_PROJECT}-global"
        )
        yaml_fingerprint = inventory_stacks.describe_stack_fingerprint(
            _session_for(yaml_client, "cloudformation"), _REGION, f"{_PROJECT}-global"
        )

        assert json_fingerprint is not None and yaml_fingerprint is not None
        assert (
            json_fingerprint["template_sha256"]
            == hashlib.sha256(b'{"Resources":{"A":2,"B":1}}').hexdigest()
        )
        assert (
            yaml_fingerprint["template_sha256"]
            == hashlib.sha256(b"Resources:\n  A: {}\n").hexdigest()
        )
        assert json_fingerprint["stack_policy"] == {"Statement": []}
        assert json_fingerprint["physical_resources"] == []
        json_client.get_template.assert_called_once_with(
            StackName=self._STACK_ID, TemplateStage="Original"
        )

    def test_fingerprint_treats_a_missing_stack_policy_as_none_but_propagates_other_errors(
        self,
    ) -> None:
        client = self._fingerprint_client({})
        client.get_stack_policy.side_effect = _client_error(
            "ValidationError", "Stack policy does not exist", "GetStackPolicy"
        )
        fingerprint = inventory_stacks.describe_stack_fingerprint(
            _session_for(client, "cloudformation"), _REGION, f"{_PROJECT}-global"
        )
        assert fingerprint is not None
        assert fingerprint["stack_policy"] is None

        client.get_stack_policy.side_effect = _client_error("Throttling", "slow down")
        with pytest.raises(ClientError):
            inventory_stacks.describe_stack_fingerprint(
                _session_for(client, "cloudformation"), _REGION, f"{_PROJECT}-global"
            )

    def test_project_stacks_keep_only_regions_with_project_prefixed_stacks(self) -> None:
        pages_by_region = {
            "us-west-2": [{"StackSummaries": [{"StackName": "CDKToolkit", "StackId": "cdk"}]}],
            _REGION: [
                {
                    "StackSummaries": [
                        {"StackName": f"{_PROJECT}-global", "StackId": "g"},
                        {"StackName": f"{_PROJECT}ish", "StackId": "near"},
                        {"StackName": _PROJECT, "StackId": "bare"},
                    ]
                }
            ],
        }
        clients = {
            region: _paginated_client({"list_stacks": pages})
            for region, pages in pages_by_region.items()
        }
        session = MagicMock()
        session.client.side_effect = lambda service, *, region_name: clients[region_name]

        inventory = inventory_stacks.collect_stack_inventory(
            session, ["us-west-2", _REGION, _REGION]
        )
        assert list(inventory) == [_REGION, "us-west-2"]
        assert inventory["us-west-2"] == [{"name": "CDKToolkit", "stack_id": "cdk", "status": ""}]

        project_stacks = inventory_stacks.collect_project_stacks(
            session, ["us-west-2", _REGION], _PROJECT
        )
        assert project_stacks == {
            _REGION: [
                {"name": _PROJECT, "stack_id": "bare", "status": ""},
                {"name": f"{_PROJECT}-global", "stack_id": "g", "status": ""},
            ]
        }


# ---------------------------------------------------------------------------
# inventory/ecr.py
# ---------------------------------------------------------------------------


class TestEcrInventory:
    _OCI = "application/vnd.oci.image.manifest.v1+json"
    _DOCKER = "application/vnd.docker.distribution.manifest.v2+json"

    @classmethod
    def _record(
        cls,
        digest: str = "sha256:abc",
        *,
        media_type: str | None = None,
        manifest: Any = '{"schemaVersion": 2}',
    ) -> dict[str, Any]:
        record: dict[str, Any] = {"imageId": {"imageDigest": digest}, "imageManifest": manifest}
        if media_type is not None:
            record["imageManifestMediaType"] = media_type
        return record

    def test_optional_configuration_distinguishes_absence_from_failure(self) -> None:
        client = MagicMock()
        client.get_lifecycle_policy.return_value = {"lifecyclePolicyText": '{"rules": []}'}
        assert inventory_ecr._optional_ecr_configuration(
            client,
            "get_lifecycle_policy",
            repository_name="repo",
            response_key="lifecyclePolicyText",
            not_found_code="LifecyclePolicyNotFoundException",
        ) == {"rules": []}
        client.get_lifecycle_policy.assert_called_once_with(repositoryName="repo")

        client.get_lifecycle_policy.side_effect = _client_error("LifecyclePolicyNotFoundException")
        assert (
            inventory_ecr._optional_ecr_configuration(
                client,
                "get_lifecycle_policy",
                repository_name="repo",
                response_key="lifecyclePolicyText",
                not_found_code="LifecyclePolicyNotFoundException",
            )
            is None
        )

        client.get_lifecycle_policy.side_effect = _client_error("AccessDeniedException")
        with pytest.raises(ClientError):
            inventory_ecr._optional_ecr_configuration(
                client,
                "get_lifecycle_policy",
                repository_name="repo",
                response_key="lifecyclePolicyText",
                not_found_code="LifecyclePolicyNotFoundException",
            )

    def _select(self, detail: dict[str, Any], records: list[dict[str, Any]]) -> dict[str, Any]:
        return inventory_ecr._select_manifest_record(
            repository_name="repo", digest="sha256:abc", detail=detail, records=records
        )

    def test_manifest_selection_rejects_every_ambiguous_or_incomplete_record(self) -> None:
        with pytest.raises(RuntimeError, match="unsupported native media type"):
            self._select({"imageManifestMediaType": "text/plain"}, [self._record()])

        with pytest.raises(RuntimeError, match="unexpected digest 'sha256:other'"):
            self._select({}, [self._record("sha256:other", media_type=self._OCI)])

        with pytest.raises(RuntimeError, match="unsupported media type 'text/plain'"):
            self._select({}, [self._record(media_type="text/plain")])

        with pytest.raises(RuntimeError, match="unsupported media type ''"):
            self._select({}, [self._record()])

        with pytest.raises(RuntimeError, match="omitted manifest bytes"):
            self._select({}, [self._record(media_type=self._OCI, manifest="   ")])

    def test_manifest_selection_falls_back_to_the_native_media_type(self) -> None:
        selected = self._select({"imageManifestMediaType": self._OCI}, [self._record()])

        assert selected == self._record()

    @staticmethod
    def _images_client(details: list[dict[str, Any]], batch_response: dict[str, Any]) -> MagicMock:
        client = _paginated_client({"describe_images": [{"imageDetails": details}]})
        client.batch_get_image.return_value = batch_response
        return client

    def test_repository_images_reject_incomplete_or_conflicting_describe_results(self) -> None:
        with pytest.raises(RuntimeError, match="omitted its digest"):
            inventory_ecr._collect_repository_images(
                self._images_client([{"imageTags": ["latest"]}], {}), "repo"
            )

        conflicting = [
            {"imageDigest": "sha256:abc", "imageTags": ["a"]},
            {"imageDigest": "sha256:abc", "imageTags": ["b"]},
        ]
        with pytest.raises(RuntimeError, match="conflicting details"):
            inventory_ecr._collect_repository_images(self._images_client(conflicting, {}), "repo")

    def test_repository_images_require_a_complete_manifest_lookup(self) -> None:
        details = [{"imageDigest": "sha256:abc"}, {"imageDigest": "sha256:def"}]

        client = self._images_client(
            details, {"failures": [{"imageId": {"imageDigest": "sha256:abc"}, "failureCode": "X"}]}
        )
        with pytest.raises(RuntimeError, match="manifest lookup failed for repo"):
            inventory_ecr._collect_repository_images(client, "repo")

        client = self._images_client(
            details, {"images": [self._record("sha256:zzz", media_type=self._OCI)]}
        )
        with pytest.raises(RuntimeError, match="unexpected digest 'sha256:zzz'"):
            inventory_ecr._collect_repository_images(client, "repo")

        client = self._images_client(details, {"images": [self._record(media_type=self._OCI)]})
        with pytest.raises(RuntimeError, match="omitted repo digests: sha256:def"):
            inventory_ecr._collect_repository_images(client, "repo")

    def _tag_lookup_client(self, details: list[dict[str, Any]]) -> MagicMock:
        client = MagicMock()
        client.describe_images.return_value = {"imageDetails": details}
        client.batch_get_image.return_value = {
            "images": [self._record(media_type=self._OCI)],
            "failures": [],
        }
        return client

    def _describe_tag(self, client: MagicMock) -> dict[str, Any] | None:
        return inventory_ecr.describe_ecr_image_by_tag(
            _session_for(client, "ecr"), region=_REGION, repository_name="repo", tag="run-tag"
        )

    def test_tag_lookup_propagates_unexpected_errors(self) -> None:
        client = MagicMock()
        client.describe_images.side_effect = _client_error("AccessDeniedException")

        with pytest.raises(ClientError):
            self._describe_tag(client)

    def test_tag_lookup_rejects_ambiguous_or_inconsistent_identities(self) -> None:
        detail = {"imageDigest": "sha256:abc", "imageTags": ["run-tag"]}
        with pytest.raises(RuntimeError, match="resolved to 2 images"):
            self._describe_tag(self._tag_lookup_client([detail, dict(detail)]))

        with pytest.raises(RuntimeError, match="invalid identity"):
            self._describe_tag(self._tag_lookup_client([{"imageTags": ["run-tag"]}]))

        with pytest.raises(RuntimeError, match="invalid identity"):
            self._describe_tag(
                self._tag_lookup_client([{"imageDigest": "sha256:abc", "imageTags": ["other"]}])
            )

        client = self._tag_lookup_client([detail])
        client.batch_get_image.return_value = {"failures": [{"failureCode": "ImageNotFound"}]}
        with pytest.raises(RuntimeError, match="manifest lookup failed for repo:run-tag"):
            self._describe_tag(client)

    def test_tag_lookup_returns_the_full_image_record(self) -> None:
        client = self._tag_lookup_client(
            [
                {
                    "imageDigest": "sha256:abc",
                    "imageTags": ["run-tag", "also"],
                    "imageSizeInBytes": 42,
                    "imagePushedAt": _PUSHED_AT,
                    "artifactMediaType": "application/vnd.oci.image.config.v1+json",
                }
            ]
        )

        assert self._describe_tag(client) == {
            "digest": "sha256:abc",
            "tags": ["also", "run-tag"],
            "size_bytes": 42,
            "pushed_at": _PUSHED_AT.isoformat(),
            "manifest_media_type": self._OCI,
            "artifact_media_type": "application/vnd.oci.image.config.v1+json",
            "manifest": {"schemaVersion": 2},
        }

    def test_inventory_captures_repository_configuration_and_images_per_region(self) -> None:
        clients: dict[str, MagicMock] = {}
        for region in ("us-west-2", _REGION):
            client = _paginated_client(
                {
                    "describe_repositories": [
                        {
                            "repositories": [
                                {
                                    "repositoryName": f"{_PROJECT}/zeta",
                                    "repositoryArn": f"arn:aws:ecr:{region}:{_ACCOUNT}:repository/{_PROJECT}/zeta",
                                    "registryId": _ACCOUNT,
                                    "repositoryUri": f"{_ACCOUNT}.dkr.ecr.{region}.amazonaws.com/{_PROJECT}/zeta",
                                    "createdAt": _PUSHED_AT,
                                    "imageTagMutability": "IMMUTABLE",
                                    "imageTagMutabilityExclusionFilters": [
                                        {"filter": "latest", "filterType": "WILDCARD"},
                                        {"filter": "dev-*", "filterType": "WILDCARD"},
                                    ],
                                    "imageScanningConfiguration": {"scanOnPush": True},
                                    "encryptionConfiguration": {"encryptionType": "AES256"},
                                },
                                {
                                    "repositoryName": "alpha",
                                    "repositoryArn": f"arn:aws:ecr:{region}:{_ACCOUNT}:repository/alpha",
                                },
                            ]
                        }
                    ],
                    "describe_images": lambda *, repositoryName: (
                        [
                            {
                                "imageDetails": [
                                    {
                                        "imageDigest": "sha256:abc",
                                        "imageTags": ["v1"],
                                        "imageSizeInBytes": 7,
                                        "imagePushedAt": _PUSHED_AT,
                                        "imageManifestMediaType": self._OCI,
                                    }
                                ]
                            }
                        ]
                        if repositoryName == f"{_PROJECT}/zeta"
                        else [{"imageDetails": []}]
                    ),
                }
            )
            client.list_tags_for_resource.side_effect = lambda *, resourceArn: {
                "tags": _tags(**{"gco:project": _PROJECT}) if "zeta" in resourceArn else []
            }
            client.get_lifecycle_policy.side_effect = lambda *, repositoryName: (
                {"lifecyclePolicyText": '{"rules": [1]}'}
                if repositoryName == f"{_PROJECT}/zeta"
                else (_ for _ in ()).throw(_client_error("LifecyclePolicyNotFoundException"))
            )
            client.get_repository_policy.side_effect = _client_error(
                "RepositoryPolicyNotFoundException"
            )
            client.batch_get_image.return_value = {
                "images": [self._record(media_type=self._OCI)],
                "failures": [],
            }
            clients[region] = client
        session = MagicMock()
        session.client.side_effect = lambda service, *, region_name: clients[region_name]

        inventory = inventory_ecr.collect_ecr_inventory(session, [_REGION, "us-west-2", _REGION])

        assert list(inventory) == [_REGION, "us-west-2"]
        assert [item["name"] for item in inventory[_REGION]] == ["alpha", f"{_PROJECT}/zeta"]
        assert inventory[_REGION][0] == {
            "name": "alpha",
            "arn": f"arn:aws:ecr:{_REGION}:{_ACCOUNT}:repository/alpha",
            "registry_id": "",
            "uri": "",
            "created_at": None,
            "tag_mutability": "",
            "tag_mutability_exclusions": [],
            "scan_configuration": {},
            "encryption": {},
            "lifecycle_policy": None,
            "repository_policy": None,
            "tags": {},
            "images": [],
        }
        zeta = inventory[_REGION][1]
        assert zeta["created_at"] == _PUSHED_AT.isoformat()
        assert zeta["tag_mutability"] == "IMMUTABLE"
        assert zeta["tag_mutability_exclusions"] == [
            {"filter": "dev-*", "filterType": "WILDCARD"},
            {"filter": "latest", "filterType": "WILDCARD"},
        ]
        assert zeta["scan_configuration"] == {"scanOnPush": True}
        assert zeta["encryption"] == {"encryptionType": "AES256"}
        assert zeta["lifecycle_policy"] == {"rules": [1]}
        assert zeta["repository_policy"] is None
        assert zeta["tags"] == {"gco:project": _PROJECT}
        assert zeta["images"] == [
            {
                "digest": "sha256:abc",
                "tags": ["v1"],
                "size_bytes": 7,
                "pushed_at": _PUSHED_AT.isoformat(),
                "manifest_media_type": self._OCI,
                "artifact_media_type": "",
                "manifest": {"schemaVersion": 2},
            }
        ]
        clients[_REGION].batch_get_image.assert_called_once_with(
            repositoryName=f"{_PROJECT}/zeta",
            imageIds=[{"imageDigest": "sha256:abc"}],
            acceptedMediaTypes=list(inventory_ecr._ECR_MANIFEST_MEDIA_TYPES),
        )


# ---------------------------------------------------------------------------
# inventory/project.py
# ---------------------------------------------------------------------------


class TestProjectInventory:
    def test_baseline_fingerprints_only_protected_stacks_and_orders_them(self) -> None:
        session = MagicMock()
        stack_inventory = {
            _REGION: [
                {"name": "CDKToolkit", "stack_id": "cdk-id", "status": "CREATE_COMPLETE"},
                {"name": f"{_PROJECT}-global", "stack_id": "g", "status": "CREATE_COMPLETE"},
                {"name": "GCOGitHubOIDCStack", "stack_id": "oidc-id", "status": "CREATE_COMPLETE"},
            ],
            "us-west-2": [{"name": f"{_PROJECT}-us-west-2", "stack_id": "w", "status": "X"}],
        }
        fingerprints = {
            "cdk-id": {"name": "CDKToolkit", "stack_id": "cdk-id", "template_sha256": "1"},
            "oidc-id": {
                "name": "GCOGitHubOIDCStack",
                "stack_id": "oidc-id",
                "template_sha256": "2",
            },
        }
        ecr = {_REGION: [{"name": "mirror"}]}

        with _patched_helpers(
            {
                "collect_stack_inventory": MagicMock(return_value=stack_inventory),
                "describe_stack_fingerprint": MagicMock(
                    side_effect=lambda session, region, stack_id: fingerprints[stack_id]
                ),
                "collect_ecr_inventory": MagicMock(return_value=ecr),
            }
        ) as fakes:
            baseline = inventory_project.capture_baseline(
                session,
                enabled_regions=["us-west-2", _REGION, _REGION],
                ecr_regions=[_REGION, _REGION],
                protected_stack_names=["GCOGitHubOIDCStack", "CDKToolkit"],
            )

        assert baseline == {
            "enabled_regions": [_REGION, "us-west-2"],
            "ecr_regions": [_REGION],
            "protected_stack_names": ["CDKToolkit", "GCOGitHubOIDCStack"],
            "protected_stacks": {
                _REGION: [fingerprints["cdk-id"], fingerprints["oidc-id"]],
            },
            "ecr_repositories": ecr,
        }
        assert [call.args[1:] for call in fakes["describe_stack_fingerprint"].call_args_list] == [
            (_REGION, "cdk-id"),
            (_REGION, "oidc-id"),
        ]
        fakes["collect_ecr_inventory"].assert_called_once_with(session, [_REGION, _REGION])

    def test_baseline_fails_when_a_protected_stack_vanishes_mid_fingerprint(self) -> None:
        with (
            _patched_helpers(
                {
                    "collect_stack_inventory": MagicMock(
                        return_value={_REGION: [{"name": "CDKToolkit", "stack_id": "cdk-id"}]}
                    ),
                    "describe_stack_fingerprint": MagicMock(return_value=None),
                    "collect_ecr_inventory": MagicMock(return_value={}),
                }
            ),
            pytest.raises(
                RuntimeError, match="disappeared while fingerprinting: us-east-1:CDKToolkit"
            ),
        ):
            inventory_project.capture_baseline(
                MagicMock(),
                enabled_regions=[_REGION],
                ecr_regions=[],
                protected_stack_names=["CDKToolkit"],
            )

    def test_tagged_surface_helpers_tolerate_malformed_values(self) -> None:
        assert inventory_project._tagged_ecr_surface("not-a-list") == "not-a-list"
        assert inventory_project._tagged_ecr_surface(None) is None
        assert inventory_project._image_tags("not-a-dict") == []
        assert inventory_project._image_tags({"tags": "v1"}) == []
        assert inventory_project._image_tags({"tags": ["v1"]}) == ["v1"]

    def test_comparison_passes_non_list_region_values_through(self) -> None:
        before = {"ecr_repositories": {_REGION: "opaque"}}
        assert (
            inventory_project.compare_baseline(before, {"ecr_repositories": {_REGION: "opaque"}})
            == []
        )
        assert inventory_project.compare_baseline(
            before, {"ecr_repositories": {_REGION: "other"}}
        ) == [
            {
                "category": "ecr_repositories",
                "region": _REGION,
                "before": "opaque",
                "after": "other",
            }
        ]

    @staticmethod
    def _session(*, backup_regions: list[str] | None = None) -> MagicMock:
        session = MagicMock()
        session.get_partition_for_region.return_value = "aws"
        session.get_available_regions.side_effect = lambda service, *, partition_name: (
            backup_regions
            if service == "backup" and backup_regions is not None
            else [_REGION, "us-west-2"]
        )
        return session

    @staticmethod
    def _regional(values: Mapping[str, Any]) -> Callable[..., Any]:
        return lambda session, region, project_name: values.get(region, [])

    def _scanner_fakes(self) -> dict[str, Any]:
        return {
            "collect_project_stacks": MagicMock(
                return_value={_REGION: [{"name": f"{_PROJECT}-global", "stack_id": "g"}]}
            ),
            "_list_project_tagged_resources": self._regional(
                {_REGION: [{"arn": "arn:aws:sqs:us-east-1:123456789012:gco-live-jobs", "tags": {}}]}
            ),
            "_list_eks_clusters": MagicMock(
                side_effect=lambda session, region, project_name: (
                    [f"{_PROJECT}-{_REGION}", "unrelated"] if region == _REGION else []
                )
            ),
            "_list_sqs_queues": self._regional({}),
            "_list_dynamodb_tables": self._regional({_REGION: [f"{_PROJECT}-jobs"]}),
            "_list_load_balancers": self._regional({}),
            "_list_target_groups": self._regional({}),
            "_list_instance_inventory": MagicMock(
                side_effect=lambda session, region, project_name: (
                    (["i-1"], ["i-1", "i-2"]) if region == _REGION else ([], ["i-3"])
                )
            ),
            "_list_instances": MagicMock(side_effect=AssertionError("never called directly")),
            "_list_project_ec2_networking": MagicMock(
                side_effect=lambda session, region, project_name, instance_ids: (
                    {"vpcs": ["vpc-1"] if region == _REGION else [], "subnets": []},
                    {"vpcs": ["vpc-1", "vpc-2"], "subnets": ["subnet-1"]},
                )
            ),
            "_list_project_ecr_repositories": self._regional({}),
            "_list_project_kms_keys": MagicMock(return_value=[]),
            "_list_lambda_functions": self._regional({}),
            "_list_api_gateway_v1_apis": self._regional({}),
            "_list_api_gateway_v2_apis": self._regional({}),
            "_list_cloudwatch_log_groups": self._regional({}),
            "_list_secrets": self._regional({}),
            "_list_cluster_volumes": self._regional({_REGION: ["vol-1"]}),
            "_list_project_backup_resources": MagicMock(
                return_value={
                    "backup_vaults": ["vault-arn"],
                    "backup_plans": [],
                    "backup_selections": [],
                    "backup_recovery_points": [],
                }
            ),
            "_list_project_s3_buckets": MagicMock(return_value=[f"{_PROJECT}-assets"]),
            "_list_project_iam_resources": MagicMock(
                return_value={
                    "iam_roles": ["role-arn"],
                    "iam_policies": [],
                    "iam_instance_profiles": [],
                    "iam_users": [],
                    "iam_groups": [],
                }
            ),
            "_global_accelerator_control_region": MagicMock(return_value="us-west-2"),
            "_list_global_accelerators": MagicMock(return_value=["accelerator-arn"]),
        }

    def test_collection_fails_closed_on_partition_or_account_problems(self) -> None:
        session = MagicMock()
        session.get_partition_for_region.return_value = ""
        with pytest.raises(RuntimeError, match="Could not resolve AWS partition"):
            inventory_project.collect_project_resources(
                session,
                enabled_regions=[_REGION],
                expected_account=_ACCOUNT,
                project_name=_PROJECT,
                seed_region=_REGION,
            )

        session.get_partition_for_region.return_value = "aws"
        with pytest.raises(RuntimeError, match="exact 12-digit account ID"):
            inventory_project.collect_project_resources(
                session,
                enabled_regions=[_REGION],
                expected_account="12345",
                project_name=_PROJECT,
                seed_region=_REGION,
            )
        session.get_available_regions.assert_not_called()

    def test_collection_runs_every_scanner_and_keeps_authority_separate(self) -> None:
        session = self._session(backup_regions=[_REGION])
        fakes = self._scanner_fakes()

        with _patched_helpers(fakes):
            inventory = inventory_project.collect_project_resources(
                session,
                enabled_regions=["us-west-2", _REGION],
                expected_account=_ACCOUNT,
                project_name=_PROJECT,
                seed_region=_REGION,
                validation_run_id="run-123",
            )

        coverage = inventory["coverage"]
        assert coverage["complete"] is True
        assert coverage["completed_scanners"] == list(inventory_shared._PROJECT_RESOURCE_SCANNERS)
        assert coverage["enabled_regions"] == [_REGION, "us-west-2"]
        assert coverage["scanner_regions"]["aws_backup"] == [_REGION]
        assert coverage["scanner_regions"]["ec2_networking"] == [_REGION, "us-west-2"]
        assert coverage["scanner_regions"]["s3_buckets"] == ["global"]
        assert coverage["scanner_regions"]["global_accelerators"] == ["us-west-2"]
        assert inventory["authority_scope"] == {"partition": "aws", "account": _ACCOUNT}
        assert inventory["cloudformation_stacks"] == {
            _REGION: [{"name": f"{_PROJECT}-global", "stack_id": "g"}]
        }
        assert inventory["authoritative_eks_clusters"] == {
            _REGION: [f"{_PROJECT}-{_REGION}", "unrelated"],
            "us-west-2": [],
        }
        assert inventory["authoritative_ec2_resources"] == {
            _REGION: {
                "instances": ["i-1", "i-2"],
                "vpcs": ["vpc-1", "vpc-2"],
                "subnets": ["subnet-1"],
            },
            "us-west-2": {
                "instances": ["i-3"],
                "vpcs": ["vpc-1", "vpc-2"],
                "subnets": ["subnet-1"],
            },
        }
        # The Region with nothing owned is dropped from the owned view but kept
        # in the authoritative maps above.
        assert list(inventory["regional"]) == [_REGION]
        east = inventory["regional"][_REGION]
        assert east["eks_clusters"] == [f"{_PROJECT}-{_REGION}"]
        assert east["instances"] == ["i-1"]
        assert east["vpcs"] == ["vpc-1"]
        assert east["dynamodb_tables"] == [f"{_PROJECT}-jobs"]
        assert east["cluster_volumes"] == ["vol-1"]
        assert east["backup_vaults"] == ["vault-arn"]
        assert east["sqs_queues"] == []
        assert inventory["s3_buckets"] == [f"{_PROJECT}-assets"]
        assert inventory["iam_roles"] == ["role-arn"]
        assert inventory["global_accelerators"] == ["accelerator-arn"]
        fakes["_list_project_kms_keys"].assert_any_call(session, _REGION, _PROJECT, "run-123")
        fakes["_list_project_ec2_networking"].assert_any_call(session, _REGION, _PROJECT, ["i-1"])
        fakes["_list_project_backup_resources"].assert_called_once_with(session, _REGION, _PROJECT)
        fakes["_list_global_accelerators"].assert_called_once_with(session, "us-west-2", _PROJECT)
        fakes["_list_instances"].assert_not_called()

    def test_collection_without_a_global_accelerator_region_records_no_scan_region(self) -> None:
        fakes = self._scanner_fakes()
        fakes["_global_accelerator_control_region"] = MagicMock(return_value=None)
        fakes["_list_global_accelerators"] = MagicMock(return_value=[])

        with _patched_helpers(fakes):
            inventory = inventory_project.collect_project_resources(
                self._session(),
                enabled_regions=[_REGION],
                expected_account=_ACCOUNT,
                project_name=_PROJECT,
                seed_region=_REGION,
            )

        assert inventory["coverage"]["scanner_regions"]["global_accelerators"] == []
        assert inventory["global_accelerators"] == []
        fakes["_list_global_accelerators"].assert_called_once()
        assert fakes["_list_global_accelerators"].call_args.args[1] is None

    def test_collection_refuses_to_report_an_incomplete_scanner_set(self) -> None:
        with (
            _patched_helpers(self._scanner_fakes()),
            patch.object(
                inventory_project,
                "_PROJECT_RESOURCE_SCANNERS",
                (*inventory_shared._PROJECT_RESOURCE_SCANNERS, "future_scanner"),
            ),
            pytest.raises(RuntimeError, match="did not run every required scanner"),
        ):
            inventory_project.collect_project_resources(
                self._session(),
                enabled_regions=[_REGION],
                expected_account=_ACCOUNT,
                project_name=_PROJECT,
                seed_region=_REGION,
            )

    @pytest.mark.parametrize(
        "mutation",
        ["no-coverage", "incomplete", "required", "completed", "categories"],
    )
    def test_absence_requires_exact_complete_coverage_metadata(self, mutation: str) -> None:
        inventory: dict[str, Any] = {
            "coverage": {
                "complete": True,
                "required_scanners": list(inventory_shared._PROJECT_RESOURCE_SCANNERS),
                "completed_scanners": list(inventory_shared._PROJECT_RESOURCE_SCANNERS),
                "resource_categories": list(inventory_shared._PROJECT_RESOURCE_CATEGORIES),
            },
            "cloudformation_stacks": {},
            "regional": {},
        }
        assert inventory_project.project_resources_are_absent(inventory) is True

        coverage = inventory["coverage"]
        if mutation == "no-coverage":
            inventory["coverage"] = "complete"
        elif mutation == "incomplete":
            coverage["complete"] = "true"
        elif mutation == "required":
            coverage["required_scanners"] = coverage["required_scanners"][:-1]
        elif mutation == "completed":
            coverage["completed_scanners"] = list(reversed(coverage["completed_scanners"]))
        else:
            coverage["resource_categories"] = [*coverage["resource_categories"], "extra"]

        assert inventory_project.project_resources_are_absent(inventory) is False


# ---------------------------------------------------------------------------
# protected.py
# ---------------------------------------------------------------------------


class TestProtectedBaselineIndexing:
    _STACK_ID = f"arn:aws:cloudformation:{_REGION}:{_ACCOUNT}:stack/GCOGitHubOIDCStack/uuid"

    def _baseline(self, **overrides: Any) -> dict[str, Any]:
        stack: dict[str, Any] = {
            "name": "GCOGitHubOIDCStack",
            "stack_id": self._STACK_ID,
            "physical_resources": [
                {
                    "logical_id": "Role",
                    "resource_type": "AWS::IAM::Role",
                    "physical_id": "gco-protected-role",
                },
                {
                    "logical_id": "OtherRole",
                    "resource_type": "AWS::IAM::Role",
                    "physical_id": "gco-protected-role-2",
                },
            ],
        }
        stack.update(overrides)
        return {"protected_stacks": {_REGION: [stack]}}

    def test_indexes_exact_stack_ids_and_physical_ids_by_type(self) -> None:
        stack_ids, resource_ids = protected._baseline_protected_identities(self._baseline())

        assert stack_ids == {_REGION: {self._STACK_ID}}
        assert resource_ids == {
            _REGION: {"AWS::IAM::Role": {"gco-protected-role", "gco-protected-role-2"}}
        }

    @pytest.mark.parametrize("baseline", [{}, {"protected_stacks": None}])
    def test_absent_protected_stacks_index_nothing(self, baseline: dict[str, Any]) -> None:
        assert protected._baseline_protected_identities(baseline) == ({}, {})

    @pytest.mark.parametrize(
        ("baseline", "message"),
        [
            ({"protected_stacks": []}, "must be an object"),
            ({"protected_stacks": {_REGION: {"not": "a list"}}}, "must be a list"),
            (
                {"protected_stacks": {_REGION: [{"stack_id": "s", "physical_resources": {}}]}},
                "omitted physical resources",
            ),
            ({"protected_stacks": {_REGION: ["not-a-dict"]}}, "is malformed"),
            (
                {"protected_stacks": {_REGION: [{"name": "x", "physical_resources": []}]}},
                "omitted its stack ID",
            ),
            (
                {"protected_stacks": {_REGION: [{"stack_id": "s", "physical_resources": ["x"]}]}},
                "has a malformed resource",
            ),
            (
                {
                    "protected_stacks": {
                        _REGION: [
                            {
                                "stack_id": "s",
                                "physical_resources": [
                                    {"logical_id": "L", "resource_type": "AWS::IAM::Role"}
                                ],
                            }
                        ]
                    }
                },
                "has an incomplete resource",
            ),
        ],
    )
    def test_malformed_baselines_are_rejected_instead_of_widening_scope(
        self, baseline: dict[str, Any], message: str
    ) -> None:
        with pytest.raises(RuntimeError, match=message):
            protected._baseline_protected_identities(baseline)


class TestProtectedArnParsers:
    @pytest.mark.parametrize(
        ("arn", "kind", "expected"),
        [
            (f"arn:aws:iam::{_ACCOUNT}:role/gco-role", "role", "gco-role"),
            (f"arn:aws:iam::{_ACCOUNT}:role/service/nested/gco-role", "role", "gco-role"),
            (f"arn:aws:iam::{_ACCOUNT}:policy/gco-role", "role", None),
            (f"arn:aws:iam::{_ACCOUNT}:role/", "role", None),
            (f"arn:aws:sts::{_ACCOUNT}:assumed-role/gco-role/session", "role", None),
            ("role/gco-role", "role", None),
        ],
    )
    def test_iam_arn_name(self, arn: str, kind: str, expected: str | None) -> None:
        assert protected._iam_arn_name(arn, kind) == expected

    @pytest.mark.parametrize(
        ("arn", "expected"),
        [
            (f"arn:aws:lambda:{_REGION}:{_ACCOUNT}:function:gco-fn", "gco-fn"),
            (f"arn:aws:lambda:{_REGION}:{_ACCOUNT}:function:gco-fn:$LATEST", None),
            (f"arn:aws:lambda:{_REGION}:{_ACCOUNT}:function:", None),
            (f"arn:aws:lambda:{_REGION}:{_ACCOUNT}:layer:gco-layer", None),
            (f"arn:aws:iam::{_ACCOUNT}:function:gco-fn", None),
        ],
    )
    def test_lambda_arn_name(self, arn: str, expected: str | None) -> None:
        assert protected._lambda_arn_name(arn) == expected

    @pytest.mark.parametrize(
        ("arn", "prefix", "expected"),
        [
            (f"arn:aws:backup:{_REGION}:{_ACCOUNT}:backup-plan:plan-1", "backup-plan:", "plan-1"),
            (f"arn:aws:backup:{_REGION}:{_ACCOUNT}:backup-vault:v", "backup-plan:", None),
            (f"arn:aws:backup:{_REGION}:{_ACCOUNT}:backup-plan:", "backup-plan:", None),
            (f"arn:aws:ec2:{_REGION}:{_ACCOUNT}:backup-plan:plan-1", "backup-plan:", None),
            ("backup-plan:plan-1", "backup-plan:", None),
        ],
    )
    def test_backup_arn_physical_id(self, arn: str, prefix: str, expected: str | None) -> None:
        assert protected._backup_arn_physical_id(arn, prefix) == expected

    @pytest.mark.parametrize(
        ("physical_id", "partition", "expected"),
        [
            ("gco-queue", "aws", "gco-queue"),
            ("", "aws", None),
            (f"https://sqs.{_REGION}.amazonaws.com/{_ACCOUNT}/gco-queue", "aws", "gco-queue"),
            (f"https://sqs.{_REGION}.amazonaws.com.cn/{_ACCOUNT}/gco-queue", "aws-cn", "gco-queue"),
            (f"https://sqs.{_REGION}.amazonaws.com/{_ACCOUNT}/gco-queue", "aws-other", None),
            (f"http://sqs.{_REGION}.amazonaws.com/{_ACCOUNT}/gco-queue", "aws", None),
            (f"https://sqs.us-west-2.amazonaws.com/{_ACCOUNT}/gco-queue", "aws", None),
            (f"https://sqs.{_REGION}.amazonaws.com/{_ACCOUNT}/gco-queue?x=1", "aws", None),
            (f"https://sqs.{_REGION}.amazonaws.com/{_ACCOUNT}/gco-queue#frag", "aws", None),
            (f"https://sqs.{_REGION}.amazonaws.com/999999999999/gco-queue", "aws", None),
            (f"https://sqs.{_REGION}.amazonaws.com/{_ACCOUNT}/gco-queue/extra", "aws", None),
            (f"https://sqs.{_REGION}.amazonaws.com/{_ACCOUNT}/", "aws", None),
        ],
    )
    def test_sqs_queue_name_normalization(
        self, physical_id: str, partition: str, expected: str | None
    ) -> None:
        assert (
            protected._sqs_queue_name_from_physical_id(
                physical_id, partition=partition, region=_REGION, account_id=_ACCOUNT
            )
            == expected
        )


class TestProtectedTaggedArnMatching:
    def _matches(self, resource_type: str, arn: str, physical_id: str, **scope: str) -> bool:
        return protected._tagged_arn_matches_protected_physical_id(
            resource_type,
            arn,
            physical_id,
            expected_partition=scope.get("partition", "aws"),
            expected_region=scope.get("region", _REGION),
            expected_account=scope.get("account", _ACCOUNT),
        )

    def test_identical_arn_matches_regardless_of_type(self) -> None:
        arn = f"arn:aws:elasticloadbalancing:{_REGION}:{_ACCOUNT}:targetgroup/tg/1"
        assert self._matches("AWS::ElasticLoadBalancingV2::TargetGroup", arn, arn) is True

    def test_malformed_arns_and_unknown_types_never_match(self) -> None:
        assert self._matches("AWS::Lambda::Function", "not-an-arn", "fn") is False
        assert (
            self._matches(
                "AWS::Lambda::Function", f"urn:aws:lambda:{_REGION}:{_ACCOUNT}:function:fn", "fn"
            )
            is False
        )
        assert (
            self._matches(
                "AWS::ECR::Repository",
                f"arn:aws:ecr:{_REGION}:{_ACCOUNT}:repository/gco/protected",
                "gco/protected",
            )
            is False
        )

    def test_dynamodb_s3_and_kms_require_exact_single_segment_resources(self) -> None:
        table = f"arn:aws:dynamodb:{_REGION}:{_ACCOUNT}:table/gco-table"
        assert self._matches("AWS::DynamoDB::Table", table, "gco-table") is True
        assert self._matches("AWS::DynamoDB::Table", f"{table}/stream/2026", "gco-table") is False
        assert (
            self._matches(
                "AWS::DynamoDB::Table",
                f"arn:aws:dax:{_REGION}:{_ACCOUNT}:table/gco-table",
                "gco-table",
            )
            is False
        )

        assert self._matches("AWS::S3::Bucket", "arn:aws:s3:::gco-bucket", "gco-bucket") is True
        assert (
            self._matches("AWS::S3::Bucket", "arn:aws:s3:::gco-bucket/key", "gco-bucket") is False
        )
        assert self._matches("AWS::S3::Bucket", "arn:aws:s3:::", "") is False

        key = f"arn:aws:kms:{_REGION}:{_ACCOUNT}:key/abc"
        assert self._matches("AWS::KMS::Key", key, "abc") is True
        assert (
            self._matches("AWS::KMS::Key", f"arn:aws:kms:{_REGION}:{_ACCOUNT}:alias/abc", "abc")
            is False
        )

    def test_sqs_matches_only_queue_arns_with_full_scope(self) -> None:
        queue_arn = f"arn:aws:sqs:{_REGION}:{_ACCOUNT}:gco-queue"
        queue_url = f"https://sqs.{_REGION}.amazonaws.com/{_ACCOUNT}/gco-queue"
        assert self._matches("AWS::SQS::Queue", queue_arn, queue_url) is True
        assert self._matches("AWS::SQS::Queue", queue_arn, "gco-queue") is True
        assert self._matches("AWS::SQS::Queue", queue_arn, "other-queue") is False
        assert (
            self._matches(
                "AWS::SQS::Queue", f"arn:aws:sns:{_REGION}:{_ACCOUNT}:gco-queue", "gco-queue"
            )
            is False
        )
        assert (
            self._matches("AWS::SQS::Queue", f"arn:aws:sqs::{_ACCOUNT}:gco-queue", "gco-queue")
            is False
        )
        assert self._matches("AWS::SQS::Queue", f"arn:aws:sqs:{_REGION}:{_ACCOUNT}:", "") is False

    def test_ec2_networking_matches_only_inside_a_trusted_scope(self) -> None:
        nat = "nat-11111111111111111"
        arn = f"arn:aws:ec2:{_REGION}:{_ACCOUNT}:natgateway/{nat}"
        assert self._matches("AWS::EC2::NatGateway", arn, nat) is True
        assert self._matches("AWS::EC2::NatGateway", arn, nat, partition="") is False
        assert self._matches("AWS::EC2::NatGateway", arn, nat, region="") is False
        assert self._matches("AWS::EC2::NatGateway", arn, nat, account="12345") is False
        assert self._matches("AWS::EC2::FlowLog", arn, nat) is False


class TestProtectedTaggedRecordMatching:
    _STACK_ID = f"arn:aws:cloudformation:{_REGION}:{_ACCOUNT}:stack/Protected/uuid"

    def _protected(self, record: Any, **kwargs: Any) -> bool:
        return protected._tagged_resource_is_protected(
            record,
            protected_stack_ids=kwargs.get("stack_ids", {self._STACK_ID}),
            protected_resource_ids=kwargs.get(
                "resource_ids", {"AWS::Lambda::Function": {"gco-protected-fn"}}
            ),
            exact_arns=kwargs.get("exact_arns", {"arn:aws:s3:::exact"}),
            expected_partition="aws",
            expected_region=_REGION,
            expected_account=_ACCOUNT,
        )

    def test_non_records_and_arnless_records_are_never_protected(self) -> None:
        assert self._protected("arn:aws:s3:::exact") is False
        assert self._protected(["arn:aws:s3:::exact"]) is False
        assert self._protected({"tags": {}}) is False
        assert self._protected({"arn": ""}) is False

    def test_exact_arn_stack_tag_or_physical_identity_protect_a_record(self) -> None:
        function_arn = f"arn:aws:lambda:{_REGION}:{_ACCOUNT}:function:gco-protected-fn"
        assert self._protected({"arn": "arn:aws:s3:::exact", "tags": {}}) is True
        assert (
            self._protected(
                {
                    "arn": "arn:aws:s3:::other",
                    "tags": {"aws:cloudformation:stack-id": self._STACK_ID},
                }
            )
            is True
        )
        assert self._protected({"arn": function_arn, "tags": "not-a-mapping"}) is True
        assert self._protected({"arn": function_arn}) is True
        assert (
            self._protected(
                {"arn": f"{function_arn}-other", "tags": {"aws:cloudformation:stack-id": "near"}}
            )
            is False
        )


class TestProtectedPhysicalIdentityMatching:
    def _matches(
        self, resource_type: str, category: str, candidate: Any, physical_id: str, **kw: Any
    ) -> bool:
        return protected._matches_protected_physical_identity(
            resource_type, category, candidate, physical_id, **kw
        )

    def test_kms_records_match_by_key_id_or_arn_only_as_objects(self) -> None:
        key_arn = f"arn:aws:kms:{_REGION}:{_ACCOUNT}:key/abc"
        assert self._matches("AWS::KMS::Key", "kms_keys", "abc", "abc") is False
        assert self._matches("AWS::KMS::Key", "kms_keys", {"key_id": "abc"}, "abc") is True
        assert self._matches("AWS::KMS::Key", "kms_keys", {"arn": key_arn}, key_arn) is True
        assert self._matches("AWS::KMS::Key", "kms_keys", {"key_id": "abcd"}, "abc") is False

    def test_non_string_candidates_never_match(self) -> None:
        assert self._matches("AWS::IAM::Role", "iam_roles", {"arn": "x"}, "x") is False
        assert self._matches("AWS::IAM::Role", "iam_roles", None, "") is False

    def test_backup_selections_need_a_protected_plan(self) -> None:
        assert (
            self._matches(
                "AWS::Backup::BackupSelection",
                "backup_selections",
                "plan-1:sel-1",
                "sel-1",
                protected_backup_plan_ids={"plan-1"},
            )
            is True
        )
        assert (
            self._matches(
                "AWS::Backup::BackupSelection", "backup_selections", "plan-1:sel-1", "sel-1"
            )
            is False
        )
        assert (
            self._matches(
                "AWS::Backup::BackupSelection",
                "backup_selections",
                "sel-1",
                "sel-1",
                protected_backup_plan_ids={"plan-1"},
            )
            is False
        )

    def test_arn_shaped_candidates_match_their_exact_physical_names(self) -> None:
        assert (
            self._matches("AWS::EKS::Cluster", "eks_clusters", "gco-cluster", "gco-cluster") is True
        )
        assert (
            self._matches(
                "AWS::Backup::BackupVault",
                "backup_vaults",
                f"arn:aws:backup:{_REGION}:{_ACCOUNT}:backup-vault:vault",
                "vault",
            )
            is True
        )
        assert (
            self._matches(
                "AWS::IAM::Role",
                "iam_roles",
                f"arn:aws:iam::{_ACCOUNT}:role/path/gco-role",
                "gco-role",
            )
            is True
        )
        assert (
            self._matches(
                "AWS::Lambda::Function",
                "lambda_functions",
                f"arn:aws:lambda:{_REGION}:{_ACCOUNT}:function:gco-fn",
                "gco-fn",
            )
            is True
        )
        assert (
            self._matches(
                "AWS::Lambda::Function",
                "lambda_functions",
                f"arn:aws:lambda:{_REGION}:{_ACCOUNT}:function:gco-fn-2",
                "gco-fn",
            )
            is False
        )
        # Types without an ARN grammar fall back to exact equality only.
        assert (
            self._matches("AWS::EKS::Cluster", "eks_clusters", "gco-cluster-2", "gco-cluster")
            is False
        )


class TestProtectedScopedIdentities:
    def _pod_cluster(self, arn: str) -> str | None:
        return protected._eks_pod_parent_cluster(arn, _REGION, "aws", _ACCOUNT)

    def test_eks_pod_identities_must_be_canonical_and_in_scope(self) -> None:
        pod = (
            f"arn:aws:eks:{_REGION}:{_ACCOUNT}:pod/gco-cluster/default/api-0/"
            "11111111-1111-1111-1111-111111111111"
        )
        association = f"arn:aws:eks:{_REGION}:{_ACCOUNT}:podidentityassociation/gco-cluster/a-11111111111111111"
        assert self._pod_cluster(pod) == "gco-cluster"
        assert self._pod_cluster(association) == "gco-cluster"
        assert self._pod_cluster(pod.replace("arn:aws:", "arn:aws-cn:")) is None
        assert self._pod_cluster(f"arn:aws:eks:{_REGION}:{_ACCOUNT}:pod//default/api-0/x") is None
        assert (
            self._pod_cluster(f"arn:aws:eks:{_REGION}:{_ACCOUNT}:pod/gco-cluster/default") is None
        )
        assert self._pod_cluster(pod.replace("/default/", "/Default/")) is None
        assert self._pod_cluster(f"{association}/extra") is None
        assert self._pod_cluster(association.replace("a-11111111111111111", "a-short")) is None
        assert self._pod_cluster(f"arn:aws:eks:{_REGION}:{_ACCOUNT}:cluster/gco-cluster") is None

    def test_kubernetes_dns_subdomains_are_validated_label_by_label(self) -> None:
        assert protected._valid_kubernetes_dns_subdomain("api-0.gco.svc") is True
        assert protected._valid_kubernetes_dns_subdomain("") is False
        assert protected._valid_kubernetes_dns_subdomain("a" * 254) is False
        assert protected._valid_kubernetes_dns_subdomain("api..gco") is False
        assert protected._valid_kubernetes_dns_subdomain("Api") is False

    @pytest.mark.parametrize(
        ("arn", "expected"),
        [
            (
                f"arn:aws:ec2:{_REGION}:{_ACCOUNT}:subnet/subnet-11111111",
                ("subnets", "subnet-11111111"),
            ),
            (
                f"arn:aws:ec2:{_REGION}:{_ACCOUNT}:vpc-flow-log/fl-11111111111111111",
                ("flow_logs", "fl-11111111111111111"),
            ),
            (f"arn:aws:ec2:{_REGION}:{_ACCOUNT}:subnet", None),
            (f"arn:aws:ec2:{_REGION}:{_ACCOUNT}:subnet/", None),
            (f"arn:aws:ec2:{_REGION}:{_ACCOUNT}:subnet/subnet-11111111/extra", None),
            (f"arn:aws:ec2:{_REGION}:{_ACCOUNT}:volume/vol-11111111", None),
            (f"arn:aws:ec2:{_REGION}:{_ACCOUNT}:subnet/subnet-1111111", None),
            (f"arn:aws:ec2:{_REGION}:{_ACCOUNT}:subnet/vpc-11111111", None),
            (f"arn:aws:ec2:us-west-2:{_ACCOUNT}:subnet/subnet-11111111", None),
            (f"arn:aws:ec2:{_REGION}:999999999999:subnet/subnet-11111111", None),
            ("arn:aws:s3:::subnet/subnet-11111111", None),
        ],
    )
    def test_ec2_tagged_identities_are_mapped_only_when_canonical(
        self, arn: str, expected: tuple[str, str] | None
    ) -> None:
        assert protected._ec2_tagged_resource_identity(arn, _REGION, "aws", _ACCOUNT) == expected


# ---------------------------------------------------------------------------
# cleanup/ecr.py
# ---------------------------------------------------------------------------


class TestEcrCleanup:
    _IDENTITY = {
        "digest": "sha256:abc",
        "manifest_media_type": "application/vnd.oci.image.manifest.v1+json",
        "artifact_media_type": "",
        "manifest": {"schemaVersion": 2},
    }

    def _image_record(self) -> dict[str, Any]:
        return {
            "region": _REGION,
            "repository": "baseline/repository",
            "tag": "run-tag",
            "identity": dict(self._IDENTITY),
        }

    def test_absent_tag_delta_is_reported_without_a_lookup_failure(self) -> None:
        ctx = _context(state={"retained_ecr_image_deltas": [self._image_record()]})

        with patch_live_validation_helper("describe_ecr_image_by_tag", return_value=None):
            result = cleanup_ecr._cleanup_new_ecr_images(ctx)

        assert result == {
            "images": [{"image": f"{_REGION}:baseline/repository:run-tag", "already_absent": True}],
            "automatic_deletion": False,
        }

    def test_changed_tag_identity_is_refused(self) -> None:
        ctx = _context(state={"retained_ecr_image_deltas": [self._image_record()]})
        repointed = {**self._IDENTITY, "digest": "sha256:other", "tags": ["run-tag"]}

        with (
            patch_live_validation_helper("describe_ecr_image_by_tag", return_value=repointed),
            pytest.raises(
                RuntimeError,
                match="image identity changed for us-east-1:baseline/repository:run-tag",
            ),
        ):
            cleanup_ecr._cleanup_new_ecr_images(ctx)

    @staticmethod
    def _repository() -> dict[str, Any]:
        return {
            "name": f"{_PROJECT}/new",
            "arn": f"arn:aws:ecr:{_REGION}:{_ACCOUNT}:repository/{_PROJECT}/new",
            "registry_id": _ACCOUNT,
            "created_at": "2026-07-17T00:00:00+00:00",
            "tags": {constants._RUN_STACK_TAG: "run-123"},
            "images": [],
        }

    def _repository_record(self, repository: dict[str, Any]) -> dict[str, Any]:
        return {
            "region": _REGION,
            "name": repository["name"],
            "arn": repository["arn"],
            "creation_identity": {
                "name": repository["name"],
                "arn": repository["arn"],
                "registry_id": repository["registry_id"],
                "created_at": repository["created_at"],
            },
            "run_tag": "run-123",
        }

    def test_without_records_no_inventory_is_collected(self) -> None:
        ctx = _context(state={})

        with patch_live_validation_helper("collect_ecr_inventory") as collect:
            result = cleanup_ecr._cleanup_new_ecr_repositories(ctx)

        assert result == {"repositories": [], "automatic_deletion": False}
        collect.assert_not_called()

    def test_acknowledged_repository_is_retained_only_when_identity_and_tag_hold(self) -> None:
        repository = self._repository()
        record = self._repository_record(repository)
        ctx = _context(state={"created_ecr_repositories": [record]})

        with patch_live_validation_helper(
            "collect_ecr_inventory", return_value={_REGION: [repository]}
        ) as collect:
            result = cleanup_ecr._cleanup_new_ecr_repositories(ctx)

        assert result == {
            "repositories": [
                {
                    "arn": repository["arn"],
                    "retained": True,
                    "reason": "ECR has no conditional repository deletion primitive",
                }
            ],
            "automatic_deletion": False,
        }
        collect.assert_called_once_with(ctx.session, {_REGION})
        ctx.session.client.return_value.delete_repository.assert_not_called()

    def test_absent_repository_is_reported_and_replaced_repository_is_refused(self) -> None:
        repository = self._repository()
        record = self._repository_record(repository)
        ctx = _context(state={"created_ecr_repositories": [record]})

        with patch_live_validation_helper("collect_ecr_inventory", return_value={_REGION: []}):
            result = cleanup_ecr._cleanup_new_ecr_repositories(ctx)
        assert result["repositories"] == [{"arn": repository["arn"], "already_absent": True}]

        recreated = {**repository, "created_at": "2026-07-18T00:00:00+00:00"}
        with (
            patch_live_validation_helper(
                "collect_ecr_inventory", return_value={_REGION: [recreated]}
            ),
            pytest.raises(
                RuntimeError, match=f"creation identity changed for {_REGION}:{_PROJECT}/new"
            ),
        ):
            cleanup_ecr._cleanup_new_ecr_repositories(ctx)


# ---------------------------------------------------------------------------
# cleanup/retained.py
# ---------------------------------------------------------------------------


class TestRetainedKmsScheduling:
    _KEY_ID = "11111111-2222-3333-4444-555555555555"
    _ARN = f"arn:aws:kms:{_REGION}:{_ACCOUNT}:key/{_KEY_ID}"

    def _record(self, cleanup_policy: str | None = None) -> dict[str, Any]:
        record: dict[str, Any] = {
            "region": _REGION,
            "key_id": self._KEY_ID,
            "arn": self._ARN,
            "run_tag": "run-123",
        }
        if cleanup_policy is not None:
            record["cleanup_policy"] = cleanup_policy
        return record

    @staticmethod
    def _identity(ctx: Any, record: Mapping[str, Any]) -> tuple[str, str, str, str]:
        return (
            str(record["region"]),
            str(record["key_id"]),
            str(record["arn"]),
            str(record.get("cleanup_policy") or "harness-schedule"),
        )

    def _environment(
        self,
        *,
        records: list[dict[str, Any]],
        states: list[str],
        deletion_date: datetime | None = _PUSHED_AT,
        arn: str | None = None,
        tags: dict[str, str] | None = None,
    ) -> tuple[Any, MagicMock]:
        ctx = _context(state={"owned_kms_keys": records})
        kms = MagicMock(name="kms")
        kms.describe_key.side_effect = [
            {
                "KeyMetadata": {
                    "Arn": arn or self._ARN,
                    "KeyState": state,
                    "DeletionDate": deletion_date if state == "PendingDeletion" else None,
                }
            }
            for state in states
        ]
        ctx.session.client.side_effect = lambda service, *, region_name: kms
        self._tags = {constants._RUN_STACK_TAG: "run-123"} if tags is None else tags
        return ctx, kms

    def _schedule(self, ctx: Any) -> dict[str, Any]:
        with _patched_helpers(
            {
                "_validated_owned_kms_identity": self._identity,
                "_kms_tags": MagicMock(return_value=self._tags),
            }
        ):
            return cleanup_retained._schedule_retained_kms_keys(ctx)

    def test_retained_keys_require_explicit_deletion_confirmation(self) -> None:
        ctx, kms = self._environment(records=[self._record()], states=["Enabled"])
        ctx.settings.confirm_kms_key_deletion = False

        with pytest.raises(RuntimeError, match="did not confirm key deletion"):
            self._schedule(ctx)
        kms.describe_key.assert_not_called()

        # CloudFormation-deleted keys need no confirmation at all.
        ctx, kms = self._environment(
            records=[self._record("cloudformation-delete")], states=["PendingDeletion"]
        )
        ctx.settings.confirm_kms_key_deletion = False
        result = self._schedule(ctx)
        assert result["keys"][0]["cleanup_policy"] == "cloudformation-delete"
        kms.schedule_key_deletion.assert_not_called()

    def test_enabled_retained_key_is_scheduled_and_reverified(self) -> None:
        record = self._record()
        ctx, kms = self._environment(records=[record], states=["Enabled", "PendingDeletion"])

        result = self._schedule(ctx)

        kms.schedule_key_deletion.assert_called_once_with(
            KeyId=self._KEY_ID, PendingWindowInDays=constants._KMS_PENDING_WINDOW_DAYS
        )
        assert result == {
            "keys": [
                {
                    "arn": self._ARN,
                    "state": "PendingDeletion",
                    "cleanup_policy": "harness-schedule",
                    "deletion_date": _PUSHED_AT.isoformat(),
                }
            ],
            "deletion_window": {
                "harness_schedule_days": constants._KMS_PENDING_WINDOW_DAYS,
                "cloudformation_delete": "observed per key deletion_date",
            },
        }
        assert record["cleanup_policy"] == "harness-schedule"
        assert record["scheduled"] is True
        assert record["deletion_date"] == _PUSHED_AT.isoformat()
        ctx.persist.assert_called_once()

    def test_already_pending_key_is_not_rescheduled(self) -> None:
        ctx, kms = self._environment(records=[self._record()], states=["PendingDeletion"])

        result = self._schedule(ctx)

        kms.schedule_key_deletion.assert_not_called()
        assert result["keys"][0]["state"] == "PendingDeletion"

    def test_missing_key_is_recorded_as_absent_and_other_errors_propagate(self) -> None:
        record = self._record()
        ctx, kms = self._environment(records=[record], states=[])
        kms.describe_key.side_effect = _client_error("NotFoundException")

        result = self._schedule(ctx)

        assert result["keys"] == [
            {"arn": self._ARN, "cleanup_policy": "harness-schedule", "already_absent": True}
        ]
        assert record["scheduled"] is True and record["deleted"] is True
        ctx.persist.assert_called_once()

        kms.describe_key.side_effect = _client_error("KMSInternalException")
        with pytest.raises(ClientError):
            self._schedule(ctx)

    def test_identity_or_ownership_drift_refuses_to_schedule(self) -> None:
        ctx, kms = self._environment(
            records=[self._record()], states=["Enabled"], arn=f"{self._ARN}-replaced"
        )
        with pytest.raises(RuntimeError, match="KMS key ARN changed"):
            self._schedule(ctx)

        ctx, kms = self._environment(
            records=[self._record()],
            states=["Enabled"],
            tags={constants._RUN_STACK_TAG: "another-run"},
        )
        with pytest.raises(RuntimeError, match="KMS run ownership changed"):
            self._schedule(ctx)
        kms.schedule_key_deletion.assert_not_called()

    def test_unexpected_states_are_refused(self) -> None:
        ctx, kms = self._environment(records=[self._record()], states=["PendingImport"])
        with pytest.raises(RuntimeError, match="is PendingImport; refusing to schedule deletion"):
            self._schedule(ctx)
        kms.schedule_key_deletion.assert_not_called()

        ctx, kms = self._environment(
            records=[self._record("cloudformation-delete")], states=["Enabled"]
        )
        with pytest.raises(
            RuntimeError, match="Expected cloudformation-delete KMS key .* found Enabled"
        ):
            self._schedule(ctx)
        kms.schedule_key_deletion.assert_not_called()

        ctx, _kms = self._environment(
            records=[self._record()], states=["PendingDeletion"], deletion_date=None
        )
        with pytest.raises(RuntimeError, match="omitted its deletion date"):
            self._schedule(ctx)


class TestRetainedResourceCleanup:
    @staticmethod
    def _fakes(**overrides: Any) -> dict[str, Any]:
        fakes: dict[str, Any] = {
            "_cleanup_owned_log_groups": MagicMock(return_value={"log_groups": []}),
            "_cleanup_new_ecr_images": MagicMock(return_value={"images": []}),
            "_cleanup_new_ecr_repositories": MagicMock(return_value={"repositories": []}),
            "_schedule_retained_kms_keys": MagicMock(return_value={"keys": []}),
        }
        fakes.update(overrides)
        return fakes

    def test_successful_cleanup_records_every_phase_and_checkpoints_the_attempt(self) -> None:
        ctx = _context()
        fakes = self._fakes()

        with _patched_helpers(fakes):
            result = cleanup_retained._retained_resource_cleanup(ctx)

        assert result["errors"] == []
        assert result["cloudwatch_logs"] == {"log_groups": []}
        assert result["ecr_images"] == {"images": []}
        assert result["ecr_repositories"] == {"repositories": []}
        assert result["kms"] == {"keys": []}
        assert result["started_at"] <= result["ended_at"]
        assert ctx.checkpoint.state["retained_cleanup_attempts"] == [result]
        ctx.persist.assert_called_once()
        for fake in fakes.values():
            fake.assert_called_once_with(ctx)

    def test_every_phase_runs_and_failures_are_aggregated_with_partial_evidence(self) -> None:
        ctx = _context()
        log_details = {"log_groups": [{"name": "blocked"}], "errors": [{"phase": "log-groups"}]}
        fakes = self._fakes(
            _cleanup_owned_log_groups=MagicMock(
                side_effect=constants._LogGroupCleanupError("logs failed", log_details)
            ),
            _cleanup_new_ecr_images=MagicMock(side_effect=RuntimeError("image drift")),
            _schedule_retained_kms_keys=MagicMock(side_effect=ValueError("kms drift")),
        )

        with _patched_helpers(fakes), pytest.raises(RuntimeError) as raised:
            cleanup_retained._retained_resource_cleanup(ctx)

        attempt = ctx.checkpoint.state["retained_cleanup_attempts"][0]
        assert attempt["cloudwatch_logs"] == log_details
        assert attempt["cloudwatch_logs"] is not log_details
        assert "ecr_images" not in attempt
        assert attempt["ecr_repositories"] == {"repositories": []}
        assert attempt["errors"] == [
            {"phase": "cloudwatch-logs", "error": "_LogGroupCleanupError: logs failed"},
            {"phase": "ecr-images", "error": "RuntimeError: image drift"},
            {"phase": "kms", "error": "ValueError: kms drift"},
        ]
        assert str(raised.value) == "Retained resource cleanup failed: " + json.dumps(
            attempt["errors"], sort_keys=True
        )
        fakes["_cleanup_new_ecr_repositories"].assert_called_once_with(ctx)
        ctx.persist.assert_called_once()

    def test_generic_log_failure_and_repository_failure_keep_no_partial_log_evidence(self) -> None:
        ctx = _context()
        fakes = self._fakes(
            _cleanup_owned_log_groups=MagicMock(side_effect=RuntimeError("helper stack missing")),
            _cleanup_new_ecr_repositories=MagicMock(side_effect=RuntimeError("repo drift")),
        )

        with (
            _patched_helpers(fakes),
            pytest.raises(RuntimeError, match="Retained resource cleanup failed"),
        ):
            cleanup_retained._retained_resource_cleanup(ctx)

        attempt = ctx.checkpoint.state["retained_cleanup_attempts"][0]
        assert "cloudwatch_logs" not in attempt
        assert [error["phase"] for error in attempt["errors"]] == [
            "cloudwatch-logs",
            "ecr-repositories",
        ]
        assert attempt["kms"] == {"keys": []}


# ---------------------------------------------------------------------------
# cleanup/workloads.py
# ---------------------------------------------------------------------------


class TestCentralJobCleanup:
    _JOB_ID = "central-job-1"

    def _central_record(self, **overrides: Any) -> dict[str, Any]:
        record: dict[str, Any] = {
            "job_id": self._JOB_ID,
            "idempotency_key": "gco-live-validation:run-123:central",
            "job_name": "gco-live-ddb-run-123",
            "namespace": "gco-jobs",
            "target_region": _REGION,
            "transport_region": None,
        }
        record.update(overrides)
        return record

    def _run(
        self,
        ctx: Any,
        central_record: dict[str, Any],
        *,
        appearance: dict[str, Any] | None,
        terminal: tuple[dict[str, Any], list[dict[str, Any]]] | None = None,
        persisted: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        fakes: dict[str, Any] = {
            "_wait_for_central_queue_appearance": MagicMock(return_value=appearance),
            "_read_central_job_item": MagicMock(return_value=persisted or appearance),
            "_validate_central_job_identity": MagicMock(),
            "_reconcile_central_cleanup_workload": MagicMock(return_value=({"name": "w"}, False)),
        }
        if terminal is not None:
            fakes["_wait_for_central_queue_terminal"] = MagicMock(return_value=terminal)
        with _patched_helpers(fakes):
            return cleanup_workloads._cleanup_central_job(ctx, central_record)

    @pytest.mark.parametrize(
        ("status_code", "message"),
        [(404, "disappeared during cancellation"), (500, "500 upstream exploded")],
    )
    def test_unexpected_cancellation_responses_fail_closed(
        self, status_code: int, message: str
    ) -> None:
        ctx = _context()
        ctx.aws_client.make_authenticated_request.return_value = _response(
            status_code, text="upstream exploded"
        )
        central_record = self._central_record()

        with pytest.raises(RuntimeError, match=message):
            self._run(ctx, central_record, appearance=_central_job(self._JOB_ID, status="queued"))

        assert "cleanup_complete" not in central_record
        ctx.persist.assert_not_called()

    def test_conflicting_cancellation_is_recorded_and_terminal_evidence_still_required(
        self,
    ) -> None:
        ctx = _context()
        ctx.aws_client.make_authenticated_request.return_value = _response(
            409, text="already claimed"
        )
        central_record = self._central_record(transport_region="us-west-2")
        queued = _central_job(self._JOB_ID, status="queued")
        succeeded = _central_job(self._JOB_ID)
        history = [{"status": "running", "at": 1.0}, {"status": "succeeded", "at": 2.0}]

        result = self._run(
            ctx,
            central_record,
            appearance=queued,
            terminal=(succeeded, history),
            persisted=succeeded,
        )

        assert result["cancellation"] == {
            "not_cancellable": True,
            "status_code": 409,
            "detail": "already claimed",
        }
        assert result["terminal_status"] == "succeeded"
        assert result["status_history"] == history
        assert result["complete"] is True
        assert central_record["cancel_attempted"] is True
        assert central_record["status"] == "succeeded"
        assert central_record["cleanup_result"] == result
        request = ctx.aws_client.make_authenticated_request.call_args.kwargs
        assert request["method"] == "DELETE"
        assert request["path"] == (
            f"/api/v1/queue/jobs/{self._JOB_ID}?reason=live%20release%20validation%20cleanup"
        )
        assert request["target_region"] == "us-west-2"

    def test_previous_cancellation_evidence_is_preserved_for_terminal_jobs(self) -> None:
        ctx = _context()
        previous = {"accepted_before_claim": True, "response": {"message": "cancelled"}}
        central_record = self._central_record(cancellation=previous)
        cancelled = _central_job(self._JOB_ID, status="cancelled")

        with patch("time.time", return_value=42.0):
            result = self._run(ctx, central_record, appearance=cancelled)

        assert result["cancellation"] == previous
        assert result["cancellation"] is not previous
        assert result["status_history"] == [{"status": "cancelled", "at": 42.0}]
        ctx.aws_client.make_authenticated_request.assert_not_called()

    def test_non_terminal_or_inconsistent_evidence_is_never_declared_complete(self) -> None:
        ctx = _context()
        ctx.aws_client.make_authenticated_request.return_value = _response(200, {"message": "ok"})
        queued = _central_job(self._JOB_ID, status="queued")
        running = _central_job(self._JOB_ID, status="running")

        with pytest.raises(RuntimeError, match="did not become terminal"):
            self._run(ctx, self._central_record(), appearance=queued, terminal=(running, []))

        succeeded = _central_job(self._JOB_ID)
        failed = _central_job(self._JOB_ID, status="failed")
        with pytest.raises(RuntimeError, match="lacks consistent terminal DynamoDB evidence"):
            self._run(
                ctx,
                self._central_record(),
                appearance=queued,
                terminal=(succeeded, []),
                persisted=failed,
            )


class TestWorkloadCleanupBarrier:
    _JOB_ID = "central-job-1"

    def _central(self, **overrides: Any) -> dict[str, Any]:
        record: dict[str, Any] = {
            "job_id": self._JOB_ID,
            "idempotency_key": "gco-live-validation:run-123:central",
            "job_name": "gco-live-ddb-run-123",
            "namespace": "gco-jobs",
            "target_region": _REGION,
            "transport_region": None,
        }
        record.update(overrides)
        return record

    @staticmethod
    def _job(**overrides: Any) -> dict[str, Any]:
        record: dict[str, Any] = {
            "name": "gco-live-api-run-123",
            "namespace": "gco-jobs",
            "region": _REGION,
            "path": "api",
            "uid": "uid-1",
            "deleted": False,
        }
        record.update(overrides)
        return record

    @pytest.mark.parametrize(
        ("persisted_status", "checkpoint_status", "message"),
        [
            ("running", "succeeded", "is no longer terminal"),
            ("failed", "succeeded", "status changed from succeeded to failed"),
        ],
    )
    def test_previously_completed_central_cleanup_is_revalidated(
        self, persisted_status: str, checkpoint_status: str, message: str
    ) -> None:
        central = self._central(
            cleanup_complete=True,
            cleanup_result={"terminal_status": checkpoint_status, "complete": True},
        )
        ctx = _context(state={"central_jobs": [central]})
        persisted = _central_job(self._JOB_ID, status=persisted_status)

        with _patched_helpers(
            {
                "_read_central_job_item": MagicMock(return_value=persisted),
                "_validate_central_job_identity": MagicMock(),
                "_reconcile_central_cleanup_workload": MagicMock(),
            }
        ) as fakes:
            result = cleanup_workloads.cleanup_workloads(ctx)

        assert result["complete"] is False
        assert result["central_jobs"] == []
        assert result["errors"] == [
            {"resource": f"central:{self._JOB_ID}", "error": result["errors"][0]["error"]}
        ]
        assert message in result["errors"][0]["error"]
        assert result["unresolved"] == [
            {"resource": f"central:{self._JOB_ID}", "reason": result["errors"][0]["error"]}
        ]
        fakes["_reconcile_central_cleanup_workload"].assert_not_called()
        assert ctx.checkpoint.state["workload_cleanup_attempts"] == [result]

    def test_pending_central_job_is_cleaned_and_its_workload_deleted(self) -> None:
        central = self._central()
        workload = self._job(name="gco-live-ddb-run-123", path="dynamodb")
        ctx = _context(
            state={"central_jobs": [central], "jobs": [workload, self._job(deleted=True)]}
        )
        outcome = {"job_id": self._JOB_ID, "complete": True}

        def cleanup_central_job(ctx: Any, record: dict[str, Any]) -> dict[str, Any]:
            record["cleanup_complete"] = True
            return outcome

        with _patched_helpers(
            {
                "_cleanup_central_job": MagicMock(side_effect=cleanup_central_job),
                "_central_workload_record": MagicMock(return_value=workload),
                "_job_reference_identity": MagicMock(return_value=("k8s-name", "k8s-namespace")),
                "_delete_owned_job": MagicMock(return_value={"deleted": True}),
            }
        ) as fakes:
            result = cleanup_workloads.cleanup_workloads(ctx)

        assert result["central_jobs"] == [outcome]
        assert result["jobs"] == [
            {
                "region": _REGION,
                "namespace": "k8s-namespace",
                "name": "k8s-name",
                "requested_namespace": "gco-jobs",
                "requested_name": "gco-live-ddb-run-123",
                "uid": "uid-1",
                "deletion": {"deleted": True},
            }
        ]
        assert result["errors"] == []
        # The record marked deleted is skipped entirely; the reconciled one is
        # still reported unresolved because the fake deletion never marked it.
        assert result["unresolved"] == [
            {
                "resource": f"{_REGION}:gco-jobs/gco-live-ddb-run-123",
                "reason": "UID-bound Job absence is incomplete",
            }
        ]
        assert result["complete"] is False
        fakes["_delete_owned_job"].assert_called_once_with(ctx, workload)
        fakes["_central_workload_record"].assert_called_once_with(ctx, central)

    def test_job_deletion_failures_are_preserved_as_unresolved(self) -> None:
        workload = self._job(k8s_job_name="actual-name", k8s_job_namespace="actual-ns")
        ctx = _context(state={"jobs": [workload]})

        with _patched_helpers(
            {
                "_job_reference_identity": MagicMock(return_value=("actual-name", "actual-ns")),
                "_delete_owned_job": MagicMock(side_effect=RuntimeError("UID mismatch")),
            }
        ):
            result = cleanup_workloads.cleanup_workloads(ctx)

        reference = f"{_REGION}:actual-ns/actual-name"
        assert result["jobs"] == []
        assert result["errors"] == [{"resource": reference, "error": "RuntimeError: UID mismatch"}]
        assert result["unresolved"] == [
            {"resource": reference, "reason": "RuntimeError: UID mismatch"}
        ]
        assert result["complete"] is False

    def test_failed_central_cleanup_is_reported_exactly_once(self) -> None:
        central = self._central()
        ctx = _context(state={"central_jobs": [central]})

        with _patched_helpers(
            {
                "_cleanup_central_job": MagicMock(side_effect=RuntimeError("queue never observed")),
                "_central_workload_record": MagicMock(side_effect=AssertionError("unreachable")),
            }
        ):
            result = cleanup_workloads.cleanup_workloads(ctx)

        reference = f"central:{self._JOB_ID}"
        assert result["errors"] == [
            {"resource": reference, "error": "RuntimeError: queue never observed"}
        ]
        assert result["unresolved"] == [
            {"resource": reference, "reason": "RuntimeError: queue never observed"}
        ]
        assert result["complete"] is False
        assert "cleanup_complete" not in central

    def test_central_cleanup_that_returns_without_completion_stays_unresolved(self) -> None:
        central = self._central()
        ctx = _context(state={"central_jobs": [central]})

        with _patched_helpers(
            {
                "_cleanup_central_job": MagicMock(return_value={"job_id": self._JOB_ID}),
                "_central_workload_record": MagicMock(return_value={}),
            }
        ):
            result = cleanup_workloads.cleanup_workloads(ctx)

        assert result["errors"] == []
        assert result["unresolved"] == [
            {
                "resource": f"central:{self._JOB_ID}",
                "reason": "terminal queue evidence is incomplete",
            }
        ]
        assert result["complete"] is False
        ctx.persist.assert_called_once()


# ---------------------------------------------------------------------------
# cleanup/log_groups.py
# ---------------------------------------------------------------------------


_LOG_NAME = "gco-live-provider-log"
_LOG_ARN = f"arn:aws:logs:{_REGION}:{_ACCOUNT}:log-group:{_LOG_NAME}"
_LOG_TOKEN = "a" * 32
_LOG_AUTHORITY = {
    constants._RUN_STACK_TAG: "run-123",
    constants._LOG_CLEANUP_TOKEN_TAG: _LOG_TOKEN,
}


def _log_identity(
    creation_time: int,
    *,
    run_tag: str | None = "run-123",
    token: str | None = _LOG_TOKEN,
    extra_tags: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    tags: dict[str, str] = {}
    if run_tag is not None:
        tags[constants._RUN_STACK_TAG] = run_tag
    if token is not None:
        tags[constants._LOG_CLEANUP_TOKEN_TAG] = token
    tags.update(extra_tags or {})
    return {"arn": _LOG_ARN, "creation_time": creation_time, "tags": tags}


def _log_record(name: str = _LOG_NAME, **overrides: Any) -> dict[str, Any]:
    record: dict[str, Any] = {
        "region": _REGION,
        "name": name,
        "stack_name": f"{_PROJECT}-{_REGION}",
        "stack_id": f"arn:aws:cloudformation:{_REGION}:{_ACCOUNT}:stack/{_PROJECT}-{_REGION}/id",
        "source_resource_type": "AWS::Logs::LogGroup",
        "source_logical_id": "ProviderLogGroup",
        "source_physical_id": name,
        "authority_phase": "pre-destroy",
        "run_tag": "run-123",
        "cleanup_token": _LOG_TOKEN,
        "observed_identity": _log_identity(1_750_000_000_000),
    }
    record.update(overrides)
    return record


def _retryable_observation_error() -> ClientError:
    return _client_error("ThrottlingException", "retry", "DescribeLogGroups")


class TestLogGroupAdoptionBlockers:
    def test_only_foreign_markers_block_adoption(self) -> None:
        blockers = cleanup_log_groups._log_group_adoption_blockers

        assert blockers({"tags": {}}, run_id="run-123", cleanup_token=_LOG_TOKEN) == []
        assert blockers({"tags": "junk"}, run_id="run-123", cleanup_token=_LOG_TOKEN) == []
        assert blockers(_log_identity(1), run_id="run-123", cleanup_token=_LOG_TOKEN) == []

        assert blockers(
            _log_identity(1, run_tag="run-999"), run_id="run-123", cleanup_token=_LOG_TOKEN
        ) == [f"foreign {constants._RUN_STACK_TAG}='run-999'"]
        assert blockers(
            _log_identity(1, token="b" * 32), run_id="run-123", cleanup_token=_LOG_TOKEN
        ) == [f"foreign {constants._LOG_CLEANUP_TOKEN_TAG}"]
        assert blockers(
            _log_identity(
                1,
                run_tag=None,
                token=None,
                extra_tags={
                    "aws:cloudformation:stack-name": "gco-us-east-1",
                    "aws:cloudformation:logical-id": "LogGroup",
                },
            ),
            run_id="run-123",
            cleanup_token=_LOG_TOKEN,
        ) == [
            "cloudformation-owned generation: aws:cloudformation:logical-id, "
            "aws:cloudformation:stack-name"
        ]


class TestRegeneratedLogGroupAdoption:
    def _adopt(
        self,
        ctx: Any,
        record: dict[str, Any],
        logs: MagicMock,
        *,
        generation: Mapping[str, Any],
        stack_absence: dict[str, Any] | None = None,
        observation: dict[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        fakes: dict[str, Any] = {
            "_verify_target_stack_absence": MagicMock(
                return_value=stack_absence or {"all_absent": True, "verified_at": "t0"}
            ),
        }
        if observation is not None:
            fakes["_observe_log_group_stability"] = MagicMock(return_value=observation)
        with _patched_helpers(fakes):
            return cleanup_log_groups._adopt_regenerated_log_group(
                ctx,
                record,
                logs,
                region=_REGION,
                name=_LOG_NAME,
                observed_generation=generation,
                authority_tags=_LOG_AUTHORITY,
            )

    def test_adoption_requires_stack_absence_and_an_arn_before_tagging(self) -> None:
        logs = MagicMock()
        regenerated = {"arn": _LOG_ARN, "creation_time": 2, "tags": {}}

        with pytest.raises(RuntimeError, match="requires every exact target stack to be absent"):
            self._adopt(
                _context(),
                _log_record(),
                logs,
                generation=regenerated,
                stack_absence={"all_absent": False},
            )
        with pytest.raises(RuntimeError, match="omitted its ARN"):
            self._adopt(
                _context(), _log_record(), logs, generation={"creation_time": 2, "tags": {}}
            )
        logs.tag_resource.assert_not_called()

    def test_adoption_rejects_a_stabilized_read_without_identity_or_a_corrupt_record(self) -> None:
        logs = MagicMock()
        regenerated = {"arn": _LOG_ARN, "creation_time": 2, "tags": {}}
        present = {"status": "present", "identity": None, "attempt_count": 2, "observations": []}

        with pytest.raises(RuntimeError, match="Adopted log group omitted its identity"):
            self._adopt(
                _context(), _log_record(), logs, generation=regenerated, observation=present
            )

        adopted_identity = _log_identity(2)
        corrupt = _log_record(adopted_generations="corrupt")
        with pytest.raises(RuntimeError, match="adopted_generations must be a list"):
            self._adopt(
                _context(),
                corrupt,
                logs,
                generation=regenerated,
                observation={**present, "identity": adopted_identity},
            )
        # Tagging happened before the checkpoint refused; the identity itself was
        # already rewritten because the generation is now provably this run's.
        assert logs.tag_resource.call_count == 2
        assert corrupt["observed_identity"] == adopted_identity

    def test_adoption_records_the_generation_and_stack_absence_proof(self) -> None:
        ctx = _context()
        record = _log_record()
        logs = MagicMock()
        regenerated = {"arn": _LOG_ARN, "creation_time": 2, "tags": {}}
        adopted_identity = _log_identity(2)

        identity = self._adopt(
            ctx,
            record,
            logs,
            generation=regenerated,
            observation={
                "status": "present",
                "identity": adopted_identity,
                "attempt_count": 2,
                "observations": [],
            },
        )

        assert identity == adopted_identity
        logs.tag_resource.assert_called_once_with(resourceArn=_LOG_ARN, tags=_LOG_AUTHORITY)
        assert record["adopted_generations"][0]["generation"] == {
            "arn": _LOG_ARN,
            "creation_time": 2,
        }
        assert record["adopted_generations"][0]["stack_absence_proof_at"] == "t0"
        assert record["identity_observation_history"][-1]["phase"] == "cleanup-adoption-post-tag"
        ctx.persist_callback.assert_called()


class TestLogGroupConvergence:
    def _environment(self, observations: list[Any], **record_overrides: Any) -> dict[str, Any]:
        record = _log_record(**record_overrides)
        ctx = _context(state={"owned_log_groups": [record], "log_group_cleanup_token": _LOG_TOKEN})
        normal_logs = MagicMock(name="normal-logs")
        restricted_logs = MagicMock(name="restricted-logs")
        ctx.session.client.side_effect = lambda service, *, region_name, **kwargs: normal_logs
        deleter = MagicMock(name="deleter")
        deleter.client.return_value = restricted_logs
        return {
            "ctx": ctx,
            "record": record,
            "normal_logs": normal_logs,
            "restricted_logs": restricted_logs,
            "deleter": deleter,
            "identity_mock": MagicMock(side_effect=observations),
            "sleep": MagicMock(),
        }

    def _converge(self, environment: dict[str, Any]) -> tuple[str, dict[str, Any]]:
        with (
            patch_live_validation_helper(
                "_verify_target_stack_absence",
                return_value={"all_absent": True, "verified_at": "t0"},
            ),
            patch_live_validation_helper("_log_group_identity", environment["identity_mock"]),
            patch("time.sleep", environment["sleep"]),
        ):
            return cleanup_log_groups._converge_one_log_group(
                environment["ctx"],
                environment["record"],
                _REGION,
                _LOG_NAME,
                authority_tags=_LOG_AUTHORITY,
                cleanup_token=_LOG_TOKEN,
                deleter=environment["deleter"],
            )

    def test_malformed_checkpoint_identity_is_refused_before_any_read(self) -> None:
        environment = self._environment([], observed_identity="not-a-dict")

        with pytest.raises(RuntimeError, match="checkpoint identity is malformed"):
            self._converge(environment)
        environment["identity_mock"].assert_not_called()

    def test_stable_absence_completes_without_provisioning_deletion_authority(self) -> None:
        environment = self._environment([None, None, None])

        outcome, entry = self._converge(environment)

        assert outcome == "completed"
        assert entry["already_absent"] is True
        assert entry["absence_observations"] == 3
        assert entry["original_generation_disposition"]["status"] == "already-absent-confirmed"
        assert environment["record"]["deleted"] is True
        environment["deleter"].client.assert_not_called()

    def test_unsettled_pending_stability_is_retryable(self) -> None:
        original = _log_identity(1_750_000_000_000)
        environment = self._environment([original, None, original, None, original, None])

        outcome, entry = self._converge(environment)

        assert outcome == "blocked"
        assert entry["retryable"] is True
        assert entry["delete_requested"] is False
        assert entry["original_generation_disposition"]["status"] == (
            "identity-not-stable-before-delete"
        )
        assert entry["observation"]["status"] == "unsettled"
        environment["deleter"].client.assert_not_called()

    def test_adoption_that_does_not_stabilize_is_retryable(self) -> None:
        regenerated = {"arn": _LOG_ARN, "creation_time": 1_750_000_000_999, "tags": {}}
        environment = self._environment([regenerated, regenerated, None, None, None])

        outcome, entry = self._converge(environment)

        assert outcome == "blocked"
        assert entry["retryable"] is True
        assert entry["original_generation_disposition"]["status"] == "adoption-did-not-stabilize"
        assert entry["original_generation_disposition"]["phase"] == "cleanup-adoption-post-tag"
        environment["normal_logs"].tag_resource.assert_called_once_with(
            resourceArn=_LOG_ARN, tags=_LOG_AUTHORITY
        )
        environment["restricted_logs"].delete_log_group.assert_not_called()

    def test_delete_racing_with_absence_still_converges(self) -> None:
        original = _log_identity(1_750_000_000_000)
        environment = self._environment([original, original, original, None, None, None])
        environment["restricted_logs"].delete_log_group.side_effect = _client_error(
            "ResourceNotFoundException", "gone", "DeleteLogGroup"
        )

        outcome, entry = self._converge(environment)

        assert outcome == "completed"
        assert entry["deleted"] is True
        assert entry["adopted"] is False
        assert entry["stack_id"] == environment["record"]["stack_id"]
        assert "delete_requested_at" in environment["record"]
        phases = [item["phase"] for item in environment["record"]["identity_observation_history"]]
        assert phases == [
            "cleanup-pending-stability",
            "cleanup-immediate-pre-delete",
            "cleanup-post-delete-absence",
        ]

    def test_other_delete_failures_propagate_with_the_pre_delete_read_recorded(self) -> None:
        original = _log_identity(1_750_000_000_000)
        environment = self._environment([original, original, original])
        environment["restricted_logs"].delete_log_group.side_effect = _client_error(
            "AccessDeniedException", "tag condition failed", "DeleteLogGroup"
        )

        with pytest.raises(ClientError):
            self._converge(environment)

        history = environment["record"]["identity_observation_history"]
        assert history[-1]["phase"] == "cleanup-immediate-pre-delete"
        assert environment["record"].get("deleted") is not True

    def test_tag_drift_immediately_before_delete_is_terminal(self) -> None:
        original = _log_identity(1_750_000_000_000)
        drifted = _log_identity(1_750_000_000_000, run_tag="foreign-run")
        environment = self._environment([original, original, drifted])

        outcome, entry = self._converge(environment)

        assert outcome == "blocked"
        assert entry["retryable"] is False
        assert entry["original_generation_disposition"]["status"] == (
            "authority-tag-drift-immediately-before-delete"
        )
        environment["restricted_logs"].delete_log_group.assert_not_called()

    def test_unsettled_pre_delete_read_is_retryable(self) -> None:
        original = _log_identity(1_750_000_000_000)
        environment = self._environment(
            [original, original, *(_retryable_observation_error() for _ in range(6))]
        )

        outcome, entry = self._converge(environment)

        assert outcome == "blocked"
        assert entry["retryable"] is True
        assert entry["original_generation_disposition"]["status"] == (
            "identity-not-stable-immediately-before-delete"
        )
        environment["restricted_logs"].delete_log_group.assert_not_called()

    def test_tag_drift_after_the_delete_request_is_terminal(self) -> None:
        original = _log_identity(1_750_000_000_000)
        drifted = _log_identity(1_750_000_000_000, token="b" * 32)
        environment = self._environment([original, original, original, drifted])

        outcome, entry = self._converge(environment)

        assert outcome == "blocked"
        assert entry["retryable"] is False
        assert entry["delete_requested"] is True
        assert entry["original_generation_disposition"]["status"] == (
            "authority-tag-drift-after-delete-request"
        )
        environment["restricted_logs"].delete_log_group.assert_called_once_with(
            logGroupName=_LOG_NAME
        )

    def test_unsettled_absence_after_the_delete_request_is_retryable(self) -> None:
        original = _log_identity(1_750_000_000_000)
        environment = self._environment(
            [original, original, original, *(_retryable_observation_error() for _ in range(6))]
        )

        outcome, entry = self._converge(environment)

        assert outcome == "blocked"
        assert entry["retryable"] is True
        assert entry["delete_requested"] is True
        assert entry["original_generation_disposition"]["status"] == (
            "absence-not-stable-after-delete-request"
        )

    @pytest.mark.parametrize(
        ("observation", "status", "message"),
        [
            ({"status": "present", "identity": None}, None, "omitted identity"),
            ({"status": "replacement", "identity": None}, "replacement-without-identity", None),
        ],
    )
    def test_observer_results_without_identity_never_reach_deletion(
        self, observation: dict[str, Any], status: str | None, message: str | None
    ) -> None:
        environment = self._environment([])
        outcome = {**observation, "attempt_count": 2, "observations": []}

        with patch_live_validation_helper("_observe_log_group_stability", return_value=outcome):
            if message is not None:
                with pytest.raises(RuntimeError, match=message):
                    self._converge(environment)
                return
            result, entry = self._converge(environment)

        assert result == "blocked"
        assert entry["retryable"] is True
        assert entry["original_generation_disposition"]["status"] == status
        environment["deleter"].client.assert_not_called()


class TestLogGroupCleanupOrchestration:
    def _ctx(self, records: Any, *, token: str = _LOG_TOKEN) -> Any:
        return _context(state={"owned_log_groups": records, "log_group_cleanup_token": token})

    @staticmethod
    def _identity_of(ctx: Any, record: Mapping[str, Any]) -> tuple[str, str]:
        return str(record["region"]), str(record["name"])

    def test_precondition_validation_fails_closed(self) -> None:
        validate = cleanup_log_groups._validated_log_group_cleanup_records

        with pytest.raises(RuntimeError, match="owned_log_groups must be a list"):
            validate(self._ctx({"a": 1}))

        with (
            patch_live_validation_helper(
                "_verify_target_stack_absence", return_value={"all_absent": False}
            ),
            pytest.raises(RuntimeError, match="requires every exact target stack to be absent"),
        ):
            validate(self._ctx([_log_record()]))

        with (
            patch_live_validation_helper(
                "_verify_target_stack_absence", return_value={"all_absent": True}
            ),
            pytest.raises(RuntimeError, match="cleanup token is malformed"),
        ):
            validate(self._ctx([_log_record()], token="not-hex"))

        with (
            patch_live_validation_helper(
                "_verify_target_stack_absence", return_value={"all_absent": True}
            ),
            pytest.raises(RuntimeError, match="must contain objects"),
        ):
            validate(self._ctx(["not-a-record"]))

    def test_precondition_validation_returns_records_with_their_exact_identity(self) -> None:
        records = [_log_record("a"), _log_record("b")]

        with _patched_helpers(
            {
                "_verify_target_stack_absence": MagicMock(return_value={"all_absent": True}),
                "_validated_owned_log_group_identity": self._identity_of,
            }
        ):
            validated, token = cleanup_log_groups._validated_log_group_cleanup_records(
                self._ctx(records)
            )

        assert token == _LOG_TOKEN
        assert validated == [(records[0], _REGION, "a"), (records[1], _REGION, "b")]

    def _sweep(
        self,
        ctx: Any,
        converge: Callable[..., tuple[str, dict[str, Any]]],
        *,
        helper_cleanup: Any = None,
    ) -> tuple[dict[str, Any] | None, constants._LogGroupCleanupError | None, MagicMock, MagicMock]:
        sleep = MagicMock()
        helper = MagicMock(
            return_value={"needed": False, "deleted": True}
            if helper_cleanup is None
            else helper_cleanup
        )
        if isinstance(helper_cleanup, Exception):
            helper = MagicMock(side_effect=helper_cleanup)
        converge_mock = MagicMock(side_effect=converge)
        with (
            _patched_helpers(
                {
                    "_verify_target_stack_absence": MagicMock(return_value={"all_absent": True}),
                    "_validated_owned_log_group_identity": self._identity_of,
                    "_converge_one_log_group": converge_mock,
                    "_delete_log_cleanup_helper": helper,
                }
            ),
            patch("time.sleep", sleep),
        ):
            try:
                return cleanup_log_groups._cleanup_owned_log_groups(ctx), None, converge_mock, sleep
            except constants._LogGroupCleanupError as exc:
                return None, exc, converge_mock, sleep

    @staticmethod
    def _blocked(name: str, *, retryable: bool) -> tuple[str, dict[str, Any]]:
        return (
            "blocked",
            {
                "region": _REGION,
                "name": name,
                "retryable": retryable,
                "original_generation_disposition": {"status": "identity-not-stable-before-delete"},
            },
        )

    @staticmethod
    def _completed(name: str) -> tuple[str, dict[str, Any]]:
        return ("completed", {"region": _REGION, "name": name, "deleted": True})

    def test_no_records_means_no_stack_absence_check_and_no_sweep(self) -> None:
        ctx = self._ctx([])

        with _patched_helpers(
            {
                "_verify_target_stack_absence": MagicMock(side_effect=AssertionError("unused")),
                "_converge_one_log_group": MagicMock(side_effect=AssertionError("unused")),
                "_delete_log_cleanup_helper": MagicMock(
                    return_value={"needed": False, "deleted": True}
                ),
            }
        ):
            details = cleanup_log_groups._cleanup_owned_log_groups(ctx)

        assert details == {
            "log_groups": [],
            "authorization": {"needed": False},
            "helper_stack_cleanup": {"needed": False, "deleted": True},
            "errors": [],
        }
        assert ctx.checkpoint.state["last_log_group_cleanup"] == details
        ctx.persist.assert_called_once()

    def test_retryable_blockers_earn_exactly_one_more_sweep_per_pass(self) -> None:
        ctx = self._ctx([_log_record("a"), _log_record("b")])
        seen: list[str] = []

        def converge(ctx: Any, record: Any, region: str, name: str, **kwargs: Any) -> Any:
            seen.append(name)
            if name == "a" or seen.count("b") > 1:
                return self._completed(name)
            return self._blocked(name, retryable=True)

        details, error, converge_mock, sleep = self._sweep(ctx, converge)

        assert error is None and details is not None
        assert seen == ["a", "b", "b"]
        assert [call.args for call in sleep.call_args_list] == [(2,)]
        assert details["errors"] == []
        assert [entry["name"] for entry in details["log_groups"]] == ["a", "b"]
        assert all(entry["deleted"] for entry in details["log_groups"])
        assert converge_mock.call_args.kwargs["cleanup_token"] == _LOG_TOKEN
        assert converge_mock.call_args.kwargs["authority_tags"] == _LOG_AUTHORITY

    def test_sweeps_are_bounded_and_the_last_blocked_state_is_reported(self) -> None:
        ctx = self._ctx([_log_record("a")])

        details, error, converge_mock, sleep = self._sweep(
            ctx, lambda *args, **kwargs: self._blocked("a", retryable=True)
        )

        assert details is None and error is not None
        assert converge_mock.call_count == constants._LOG_GROUP_CLEANUP_MAX_PASSES
        assert [call.args for call in sleep.call_args_list] == [(2,), (3,)]
        assert "could not converge for: us-east-1:a (identity-not-stable-before-delete)" in str(
            error
        )
        assert error.details["log_groups"] == [self._blocked("a", retryable=True)[1]]
        assert error.details["errors"][0]["phase"] == "log-groups"
        assert ctx.checkpoint.state["last_log_group_cleanup"] == error.details

    def test_non_retryable_blocker_stops_after_the_first_sweep(self) -> None:
        ctx = self._ctx([_log_record("a")])

        _details, error, converge_mock, sleep = self._sweep(
            ctx, lambda *args, **kwargs: self._blocked("a", retryable=False)
        )

        assert error is not None
        assert converge_mock.call_count == 1
        sleep.assert_not_called()

    def test_helper_teardown_failure_is_reported_even_when_convergence_succeeded(self) -> None:
        ctx = self._ctx([_log_record("a")])

        details, error, _converge, _sleep = self._sweep(
            ctx,
            lambda *args, **kwargs: self._completed("a"),
            helper_cleanup=RuntimeError("helper stack stuck in DELETE_FAILED"),
        )

        assert details is None and error is not None
        assert error.details["log_groups"] == [self._completed("a")[1]]
        assert error.details["helper_stack_cleanup"] == {"needed": False, "deleted": True}
        assert error.details["errors"] == [
            {
                "phase": "cleanup-helper",
                "error": "RuntimeError: helper stack stuck in DELETE_FAILED",
            }
        ]
        assert isinstance(error.__cause__, RuntimeError)
        assert "helper stack stuck" in str(error.__cause__)

    def test_both_independent_failures_are_reported_with_convergence_as_the_cause(self) -> None:
        ctx = self._ctx([_log_record("a")])

        _details, error, _converge, _sleep = self._sweep(
            ctx,
            lambda *args, **kwargs: self._blocked("a", retryable=False),
            helper_cleanup=RuntimeError("helper stuck"),
        )

        assert error is not None
        assert [item["phase"] for item in error.details["errors"]] == [
            "log-groups",
            "cleanup-helper",
        ]
        assert "could not converge" in str(error.__cause__)
