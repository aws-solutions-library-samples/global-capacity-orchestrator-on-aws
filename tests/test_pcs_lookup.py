"""Tests for the AWS PCS lookup custom resource handler.

``lambda/pcs-lookup/handler.py`` is the ``cr.Provider`` on-event handler
behind two resource types. ``Custom::PcsAmiLookup`` runs once per distinct
``(ami kind, architecture)`` the managed cluster's node groups ask for: Create
and Update resolve the newest AWS-published AMI whose description lists the
cluster's Slurm version (the PCS sample AMI by name search; the PCS-ready
DLAMI through its SSM public parameter first, the name search as the
fallback). ``Custom::PcsControllerLookup`` resolves the controller's
``SLURMCTLD`` endpoint by type for the metrics scrape. Delete touches
nothing, and malformed properties fail by name. Real botocore clients run
behind ``Stubber`` so every AWS call and its exact parameters are asserted
without a network.
"""

from __future__ import annotations

from collections.abc import Iterator
from types import ModuleType
from typing import Any

import boto3
import pytest
from botocore.stub import Stubber

from tests._lambda_imports import load_lambda_module

_REGION = "us-east-1"
_SAMPLE_X86 = "aws-pcs-sample_ami-al2023-x86_64*"
_DLAMI_ARM = "aws-pcs-ready-dlami-base-ubuntu2404-arm64-*"
_DLAMI_PARAMETER_ARM = "/aws/service/pcs/ami/dlami-base-ubuntu2404/arm64/latest/ami-id"


@pytest.fixture
def handler() -> ModuleType:
    return load_lambda_module("pcs-lookup")


class _Clients:
    """Stubbed EC2, SSM and PCS clients plus the factory calls seen."""

    def __init__(self) -> None:
        kwargs = {
            "region_name": _REGION,
            "aws_access_key_id": "testing",
            "aws_secret_access_key": "testing",  # nosec B106  # fake credentials for stubbed clients
        }
        self.ec2 = boto3.client("ec2", **kwargs)
        self.ssm = boto3.client("ssm", **kwargs)
        self.pcs = boto3.client("pcs", **kwargs)
        self.ec2_stub = Stubber(self.ec2)
        self.ssm_stub = Stubber(self.ssm)
        self.pcs_stub = Stubber(self.pcs)
        self.requests: list[tuple[str, dict[str, Any]]] = []

    def factory(self, service: str, **kwargs: Any) -> Any:
        self.requests.append((service, kwargs))
        return {"ec2": self.ec2, "ssm": self.ssm, "pcs": self.pcs}[service]

    def expect_describe_by_name(
        self,
        *,
        name: str,
        description: str | None,
        images: list[dict[str, Any]],
    ) -> None:
        filters = [
            {"Name": "name", "Values": [name]},
            {"Name": "state", "Values": ["available"]},
        ]
        if description is not None:
            filters.append({"Name": "description", "Values": [description]})
        self.ec2_stub.add_response(
            "describe_images",
            {"Images": images},
            {"Owners": ["amazon"], "Filters": filters},
        )


@pytest.fixture
def clients(handler: ModuleType, monkeypatch: pytest.MonkeyPatch) -> Iterator[_Clients]:
    stubbed = _Clients()
    monkeypatch.setattr(handler.boto3, "client", stubbed.factory)
    with stubbed.ec2_stub, stubbed.ssm_stub, stubbed.pcs_stub:
        yield stubbed
        stubbed.ec2_stub.assert_no_pending_responses()
        stubbed.ssm_stub.assert_no_pending_responses()
        stubbed.pcs_stub.assert_no_pending_responses()


def _image(image_id: str, created: str, *, name: str = "", description: str = "") -> dict[str, Any]:
    return {
        "ImageId": image_id,
        "CreationDate": created,
        "Name": name or f"name-of-{image_id}",
        "Description": description,
        "State": "available",
    }


def _event(request_type: str, **properties: Any) -> dict[str, Any]:
    resource_properties: dict[str, Any] = {
        "ServiceToken": "arn:aws:lambda:us-east-1:123456789012:function:provider",
        "Kind": "sample",
        "Architecture": "x86_64",
        "SlurmVersion": "25.11",
        "Region": _REGION,
        "NodeGroup": "gco-compute",
    }
    resource_properties.update(properties)
    return {
        "RequestType": request_type,
        "LogicalResourceId": "AmiLookupSampleX8664",
        "ResourceProperties": resource_properties,
    }


class TestSampleAmi:
    def test_create_resolves_the_newest_sample_ami_for_the_slurm_version(
        self, handler: ModuleType, clients: _Clients
    ) -> None:
        clients.expect_describe_by_name(
            name=_SAMPLE_X86,
            description="*25.11*",
            images=[
                _image("ami-0000000000000000a", "2026-05-01T00:00:00.000Z"),
                _image(
                    "ami-0000000000000000b",
                    "2026-07-23T00:00:00.000Z",
                    name="aws-pcs-sample_ami-al2023-x86_64-20260723",
                    description="Slurm: 25.05, 25.11, 26.05.",
                ),
                _image("ami-0000000000000000c", "2026-06-15T00:00:00.000Z"),
            ],
        )

        response = handler.lambda_handler(_event("Create"))

        assert response == {
            "PhysicalResourceId": "gco-pcs-ami-gco-compute-sample-x86_64",
            "Data": {
                "ImageId": "ami-0000000000000000b",
                "Name": "aws-pcs-sample_ami-al2023-x86_64-20260723",
                "Description": "Slurm: 25.05, 25.11, 26.05.",
            },
        }
        # Only EC2 is consulted for the sample AMI, in the event's Region.
        assert [service for service, _ in clients.requests] == ["ec2"]
        assert clients.requests[0][1]["region_name"] == _REGION

    def test_update_keeps_the_physical_id_and_re_resolves(
        self, handler: ModuleType, clients: _Clients
    ) -> None:
        clients.expect_describe_by_name(
            name=_SAMPLE_X86,
            description="*26.05*",
            images=[_image("ami-0000000000000000d", "2026-08-01T00:00:00.000Z")],
        )

        response = handler.lambda_handler(
            {**_event("Update", SlurmVersion="26.05"), "PhysicalResourceId": "gco-pcs-ami-keep"}
        )

        assert response["PhysicalResourceId"] == "gco-pcs-ami-keep"
        assert response["Data"]["ImageId"] == "ami-0000000000000000d"

    def test_without_a_slurm_version_the_description_filter_is_dropped(
        self, handler: ModuleType, clients: _Clients
    ) -> None:
        clients.expect_describe_by_name(
            name=_SAMPLE_X86,
            description=None,
            images=[_image("ami-0000000000000000e", "2026-01-01T00:00:00.000Z")],
        )

        response = handler.lambda_handler(_event("Create", SlurmVersion=""))

        assert response["Data"]["ImageId"] == "ami-0000000000000000e"

    def test_no_match_fails_the_resource(self, handler: ModuleType, clients: _Clients) -> None:
        clients.expect_describe_by_name(name=_SAMPLE_X86, description="*25.11*", images=[])

        with pytest.raises(LookupError, match=r"no available Amazon-owned AMI matches .*25\.11"):
            handler.lambda_handler(_event("Create"))

    def test_images_without_an_id_are_ignored(self, handler: ModuleType, clients: _Clients) -> None:
        clients.expect_describe_by_name(
            name=_SAMPLE_X86,
            description="*25.11*",
            images=[{"CreationDate": "2026-09-01T00:00:00.000Z", "State": "available"}],
        )

        with pytest.raises(LookupError):
            handler.lambda_handler(_event("Create"))


class TestDlami:
    def test_the_ssm_parameter_wins_when_its_image_lists_the_slurm_version(
        self, handler: ModuleType, clients: _Clients
    ) -> None:
        clients.ssm_stub.add_response(
            "get_parameter",
            {"Parameter": {"Name": _DLAMI_PARAMETER_ARM, "Value": "ami-0dlami00000000001"}},
            {"Name": _DLAMI_PARAMETER_ARM},
        )
        clients.ec2_stub.add_response(
            "describe_images",
            {
                "Images": [
                    _image(
                        "ami-0dlami00000000001",
                        "2026-05-22T00:00:00.000Z",
                        name="aws-pcs-ready-dlami-base-ubuntu2404-arm64-20260522",
                        description=(
                            "PCS-Ready DLAMI based on Deep Learning Base OSS Nvidia Driver GPU "
                            "AMI (Ubuntu 24.04) 20260522. PCS Agent: 1.5.0-1. "
                            "Slurm: 24.11.7-1, 25.05.7-1, 25.11.2-1. EFS Utils: 2.4.2"
                        ),
                    )
                ]
            },
            {"ImageIds": ["ami-0dlami00000000001"]},
        )

        response = handler.lambda_handler(
            _event("Create", Kind="dlami", Architecture="arm64", NodeGroup="gpu")
        )

        assert response["PhysicalResourceId"] == "gco-pcs-ami-gpu-dlami-arm64"
        assert response["Data"]["ImageId"] == "ami-0dlami00000000001"
        assert response["Data"]["Name"].startswith("aws-pcs-ready-dlami")
        assert [service for service, _ in clients.requests] == ["ec2", "ssm"]

    def test_a_parameter_image_without_the_slurm_version_falls_back_to_the_name_search(
        self, handler: ModuleType, clients: _Clients
    ) -> None:
        clients.ssm_stub.add_response(
            "get_parameter",
            {"Parameter": {"Name": _DLAMI_PARAMETER_ARM, "Value": "ami-0dlami00000000002"}},
            {"Name": _DLAMI_PARAMETER_ARM},
        )
        clients.ec2_stub.add_response(
            "describe_images",
            {
                "Images": [
                    _image(
                        "ami-0dlami00000000002",
                        "2026-05-22T00:00:00.000Z",
                        description="PCS Agent: 1.5.0-1. Slurm: 24.11.7-1, 25.05.7-1.",
                    )
                ]
            },
            {"ImageIds": ["ami-0dlami00000000002"]},
        )
        clients.expect_describe_by_name(
            name=_DLAMI_ARM,
            description="*26.05*",
            images=[_image("ami-0dlami00000000003", "2026-09-01T00:00:00.000Z")],
        )

        response = handler.lambda_handler(
            _event("Create", Kind="dlami", Architecture="arm64", SlurmVersion="26.05")
        )

        assert response["Data"]["ImageId"] == "ami-0dlami00000000003"

    def test_a_parameter_image_ec2_no_longer_describes_is_kept_without_a_version(
        self, handler: ModuleType, clients: _Clients
    ) -> None:
        # describe-images returns nothing for the id (a just-deregistered
        # image); with no version to check, the parameter's id stands.
        clients.ssm_stub.add_response(
            "get_parameter",
            {"Parameter": {"Name": _DLAMI_PARAMETER_ARM, "Value": "ami-0dlami00000000004"}},
            {"Name": _DLAMI_PARAMETER_ARM},
        )
        clients.ec2_stub.add_response(
            "describe_images", {"Images": []}, {"ImageIds": ["ami-0dlami00000000004"]}
        )

        response = handler.lambda_handler(
            _event("Create", Kind="dlami", Architecture="arm64", SlurmVersion="")
        )

        assert response["Data"] == {
            "ImageId": "ami-0dlami00000000004",
            "Name": "",
            "Description": "",
        }

    @pytest.mark.parametrize("code", ["ParameterNotFound", "AccessDeniedException"])
    def test_a_missing_or_denied_parameter_falls_back_to_the_name_search(
        self, handler: ModuleType, clients: _Clients, code: str
    ) -> None:
        clients.ssm_stub.add_client_error(
            "get_parameter", service_error_code=code, expected_params={"Name": _DLAMI_PARAMETER_ARM}
        )
        clients.expect_describe_by_name(
            name=_DLAMI_ARM,
            description="*25.11*",
            images=[_image("ami-0dlami00000000005", "2026-09-01T00:00:00.000Z")],
        )

        response = handler.lambda_handler(_event("Create", Kind="dlami", Architecture="arm64"))

        assert response["Data"]["ImageId"] == "ami-0dlami00000000005"

    def test_an_empty_parameter_value_falls_back_to_the_name_search(
        self, handler: ModuleType, clients: _Clients
    ) -> None:
        clients.ssm_stub.add_response(
            "get_parameter",
            {"Parameter": {"Name": _DLAMI_PARAMETER_ARM, "Value": "  "}},
            {"Name": _DLAMI_PARAMETER_ARM},
        )
        clients.expect_describe_by_name(
            name=_DLAMI_ARM,
            description="*25.11*",
            images=[_image("ami-0dlami00000000006", "2026-09-01T00:00:00.000Z")],
        )

        response = handler.lambda_handler(_event("Create", Kind="dlami", Architecture="arm64"))

        assert response["Data"]["ImageId"] == "ami-0dlami00000000006"

    def test_other_ssm_errors_propagate(self, handler: ModuleType, clients: _Clients) -> None:
        clients.ssm_stub.add_client_error(
            "get_parameter",
            service_error_code="ThrottlingException",
            expected_params={"Name": _DLAMI_PARAMETER_ARM},
        )

        with pytest.raises(handler.ClientError):
            handler.lambda_handler(_event("Create", Kind="dlami", Architecture="arm64"))


class TestEventValidation:
    def test_delete_is_a_no_op_that_keeps_the_physical_id(self, handler: ModuleType) -> None:
        # No clients are stubbed: a Delete must not touch AWS.
        assert handler.lambda_handler(
            {**_event("Delete"), "PhysicalResourceId": "gco-pcs-ami-gco-compute-sample-x86_64"}
        ) == {"PhysicalResourceId": "gco-pcs-ami-gco-compute-sample-x86_64"}
        assert handler.lambda_handler(_event("Delete")) == {"PhysicalResourceId": "gco-pcs-lookup"}

    def test_unknown_request_types_are_refused(self, handler: ModuleType) -> None:
        with pytest.raises(ValueError, match=r"Unsupported RequestType: 'Read'"):
            handler.lambda_handler(_event("Read"))

    def test_unknown_resource_types_are_refused(self, handler: ModuleType) -> None:
        with pytest.raises(ValueError, match=r"ResourceType must be one of .*got 'Custom::Other'"):
            handler.lambda_handler({**_event("Create"), "ResourceType": "Custom::Other"})

    def test_kind_architecture_and_region_are_validated_by_name(self, handler: ModuleType) -> None:
        with pytest.raises(ValueError, match=r"Kind must be one of sample, dlami, got 'ubuntu'"):
            handler.lambda_handler(_event("Create", Kind="ubuntu"))
        with pytest.raises(
            ValueError, match=r"Architecture must be one of x86_64, arm64, got 'i386'"
        ):
            handler.lambda_handler(_event("Create", Architecture="i386"))
        with pytest.raises(ValueError, match=r"Region is required"):
            handler.lambda_handler(_event("Create", Region=" "))
        with pytest.raises(ValueError, match=r"Kind must be one of"):
            handler.lambda_handler({"RequestType": "Create"})

    def test_kind_is_case_insensitive_and_the_node_group_label_defaults(
        self, handler: ModuleType, clients: _Clients
    ) -> None:
        clients.expect_describe_by_name(
            name=_SAMPLE_X86,
            description="*25.11*",
            images=[_image("ami-0000000000000000f", "2026-01-01T00:00:00.000Z")],
        )

        response = handler.lambda_handler(_event("Create", Kind=" Sample ", NodeGroup=""))

        assert response["PhysicalResourceId"] == "gco-pcs-ami-node-group-sample-x86_64"


class TestHelpers:
    def test_newest_image_orders_by_creation_date(self, handler: ModuleType) -> None:
        assert handler.newest_image([]) is None
        newest = handler.newest_image(
            [
                _image("ami-0000000000000000a", "2026-01-01T00:00:00.000Z"),
                _image("ami-0000000000000000b", "2026-03-01T00:00:00.000Z"),
                {"ImageId": "ami-0000000000000000c"},
            ]
        )
        assert newest is not None and newest["ImageId"] == "ami-0000000000000000b"

    def test_supports_version(self, handler: ModuleType) -> None:
        image = {"Description": "Slurm: 25.05, 25.11, 26.05."}
        assert handler.supports_version(image, "25.11") is True
        assert handler.supports_version(image, "24.11") is False
        assert handler.supports_version({}, "25.11") is False
        assert handler.supports_version({}, "") is True


# ---------------------------------------------------------------------------
# Custom::PcsControllerLookup
# ---------------------------------------------------------------------------

_CLUSTER_ID = "pcs_0a1b2c3d4e"


def _controller_event(request_type: str, **properties: Any) -> dict[str, Any]:
    resource_properties: dict[str, Any] = {
        "ServiceToken": "arn:aws:lambda:us-east-1:123456789012:function:provider",
        "ClusterId": _CLUSTER_ID,
        "Region": _REGION,
    }
    resource_properties.update(properties)
    return {
        "RequestType": request_type,
        "ResourceType": "Custom::PcsControllerLookup",
        "LogicalResourceId": "ControllerLookup",
        "ResourceProperties": resource_properties,
    }


def _cluster(endpoints: list[dict[str, Any]], *, status: str = "ACTIVE") -> dict[str, Any]:
    return {
        "name": "gco-pcs",
        "id": _CLUSTER_ID,
        "arn": f"arn:aws:pcs:{_REGION}:123456789012:cluster/{_CLUSTER_ID}",
        "status": status,
        "createdAt": "2026-10-01T00:00:00Z",
        "modifiedAt": "2026-10-01T00:00:00Z",
        "scheduler": {"type": "SLURM", "version": "25.11"},
        "size": "SMALL",
        "networking": {"subnetIds": ["subnet-0aaa"], "securityGroupIds": ["sg-0pcs"]},
        "endpoints": endpoints,
    }


class TestControllerLookup:
    def test_create_picks_the_slurmctld_endpoint_by_type(
        self, handler: ModuleType, clients: _Clients
    ) -> None:
        # The controller's entry is deliberately not first: order is not a contract.
        clients.pcs_stub.add_response(
            "get_cluster",
            {
                "cluster": _cluster(
                    [
                        {"type": "SLURMDBD", "privateIpAddress": "10.0.1.5", "port": "6819"},
                        {"type": "SLURMRESTD", "privateIpAddress": "10.0.1.5", "port": "6820"},
                        {"type": "SLURMCTLD", "privateIpAddress": "10.0.1.5", "port": "6817"},
                    ]
                )
            },
            {"clusterIdentifier": _CLUSTER_ID},
        )

        response = handler.lambda_handler(_controller_event("Create"))

        assert response == {
            "PhysicalResourceId": f"gco-pcs-controller-{_CLUSTER_ID}",
            "Data": {"PrivateIpAddress": "10.0.1.5", "Port": "6817", "Address": "10.0.1.5:6817"},
        }
        assert clients.requests == [
            ("pcs", {"region_name": _REGION, "config": handler._CLIENT_CONFIG})
        ]

    def test_update_keeps_the_physical_id(self, handler: ModuleType, clients: _Clients) -> None:
        clients.pcs_stub.add_response(
            "get_cluster",
            {
                "cluster": _cluster(
                    [{"type": "SLURMCTLD", "privateIpAddress": "10.0.1.9", "port": "6817"}]
                )
            },
            {"clusterIdentifier": _CLUSTER_ID},
        )

        response = handler.lambda_handler(
            {**_controller_event("Update"), "PhysicalResourceId": "kept"}
        )

        assert response["PhysicalResourceId"] == "kept"
        assert response["Data"]["Address"] == "10.0.1.9:6817"

    def test_a_cluster_without_a_controller_endpoint_fails_by_name(
        self, handler: ModuleType, clients: _Clients
    ) -> None:
        clients.pcs_stub.add_response(
            "get_cluster",
            {
                "cluster": _cluster(
                    [
                        # No private address yet (the shape requires the key).
                        {"type": "SLURMCTLD", "privateIpAddress": "", "port": "6817"},
                        {"type": "SLURMDBD", "privateIpAddress": "10.0.1.5", "port": "6819"},
                    ],
                    status="CREATING",
                )
            },
            {"clusterIdentifier": _CLUSTER_ID},
        )

        with pytest.raises(
            LookupError, match=rf"{_CLUSTER_ID} reports no SLURMCTLD endpoint.*CREATING"
        ):
            handler.lambda_handler(_controller_event("Create"))

    def test_properties_are_validated_by_name(self, handler: ModuleType) -> None:
        with pytest.raises(ValueError, match=r"ClusterId is required"):
            handler.lambda_handler(_controller_event("Create", ClusterId=" "))
        with pytest.raises(ValueError, match=r"Region is required"):
            handler.lambda_handler(_controller_event("Create", Region=""))

    def test_delete_is_a_no_op(self, handler: ModuleType) -> None:
        assert handler.lambda_handler(
            {**_controller_event("Delete"), "PhysicalResourceId": "gco-pcs-controller-x"}
        ) == {"PhysicalResourceId": "gco-pcs-controller-x"}

    def test_pcs_errors_propagate(self, handler: ModuleType, clients: _Clients) -> None:
        clients.pcs_stub.add_client_error(
            "get_cluster",
            service_error_code="ResourceNotFoundException",
            expected_params={"clusterIdentifier": _CLUSTER_ID},
        )

        with pytest.raises(handler.ClientError):
            handler.lambda_handler(_controller_event("Create"))

    def test_controller_endpoint_helper_handles_odd_documents(self, handler: ModuleType) -> None:
        assert handler.controller_endpoint(
            {"endpoints": [{"type": "slurmctld", "privateIpAddress": "10.0.0.1", "port": 6817}]}
        ) == {"PrivateIpAddress": "10.0.0.1", "Port": "6817", "Address": "10.0.0.1:6817"}
        with pytest.raises(LookupError, match=r"cluster \? reports no SLURMCTLD endpoint"):
            handler.controller_endpoint({})


# ---------------------------------------------------------------------------
# Custom::PcsSubnetLookup
# ---------------------------------------------------------------------------

_SUBNETS = {
    "subnet-0a": "us-east-1a",
    "subnet-0b": "us-east-1b",
    "subnet-0e": "us-east-1e",
    "subnet-0f": "us-east-1f",
}


def _subnet_event(request_type: str, **properties: Any) -> dict[str, Any]:
    resource_properties: dict[str, Any] = {
        "ServiceToken": "arn:aws:lambda:us-east-1:123456789012:function:provider",
        "SubnetIds": list(_SUBNETS),
        "InstanceTypes": ["t3.medium", "c6i.large"],
        "Region": _REGION,
        "NodeGroup": "gco-compute",
    }
    resource_properties.update(properties)
    return {
        "RequestType": request_type,
        "ResourceType": "Custom::PcsSubnetLookup",
        "LogicalResourceId": "AmiLookupSubnetsGcoCompute",
        "ResourceProperties": resource_properties,
    }


def _expect_describe_subnets(clients: _Clients, zones: dict[str, str]) -> None:
    clients.ec2_stub.add_response(
        "describe_subnets",
        {
            "Subnets": [
                {"SubnetId": subnet_id, "AvailabilityZone": zone, "VpcId": "vpc-0gco"}
                for subnet_id, zone in zones.items()
            ]
        },
        {"SubnetIds": list(_SUBNETS)},
    )


def _expect_offerings(
    clients: _Clients, offered: list[tuple[str, str]], *, types: list[str]
) -> None:
    clients.ec2_stub.add_response(
        "describe_instance_type_offerings",
        {
            "InstanceTypeOfferings": [
                {
                    "InstanceType": instance_type,
                    "LocationType": "availability-zone",
                    "Location": zone,
                }
                for instance_type, zone in offered
            ]
        },
        {
            "LocationType": "availability-zone",
            "Filters": [{"Name": "instance-type", "Values": types}],
        },
    )


class TestSubnetLookup:
    _TYPES = ["c6i.large", "t3.medium"]  # sorted and deduplicated from the event

    def test_create_keeps_the_zones_that_offer_every_type(
        self, handler: ModuleType, clients: _Clients
    ) -> None:
        """us-east-1e offers neither type and us-east-1f lacks one: both are dropped."""
        _expect_describe_subnets(clients, _SUBNETS)
        _expect_offerings(
            clients,
            [
                ("c6i.large", "us-east-1a"),
                ("t3.medium", "us-east-1a"),
                ("t3.medium", "us-east-1b"),
                ("c6i.large", "us-east-1b"),
                ("t3.medium", "us-east-1f"),
            ],
            types=self._TYPES,
        )

        response = handler.lambda_handler(_subnet_event("Create"))

        assert response == {
            "PhysicalResourceId": "gco-pcs-subnets-gco-compute",
            "Data": {
                "SubnetIds": ["subnet-0a", "subnet-0b"],
                "AvailabilityZones": ["us-east-1a", "us-east-1b"],
                "DroppedZones": "us-east-1e (no c6i.large, t3.medium); us-east-1f (no c6i.large)",
            },
        }
        assert clients.requests == [
            ("ec2", {"region_name": _REGION, "config": handler._CLIENT_CONFIG})
        ]

    def test_update_keeps_the_physical_id_and_the_callers_subnet_order(
        self, handler: ModuleType, clients: _Clients
    ) -> None:
        # EC2 describes the subnets in its own order; the response keeps the caller's.
        _expect_describe_subnets(clients, dict(reversed(list(_SUBNETS.items()))))
        _expect_offerings(
            clients,
            [(t, z) for z in _SUBNETS.values() for t in self._TYPES],
            types=self._TYPES,
        )

        response = handler.lambda_handler({**_subnet_event("Update"), "PhysicalResourceId": "kept"})

        assert response["PhysicalResourceId"] == "kept"
        assert response["Data"]["SubnetIds"] == list(_SUBNETS)
        assert response["Data"]["AvailabilityZones"] == sorted(_SUBNETS.values())
        assert response["Data"]["DroppedZones"] == "none"

    def test_no_zone_left_fails_the_resource_by_name(
        self, handler: ModuleType, clients: _Clients
    ) -> None:
        _expect_describe_subnets(clients, _SUBNETS)
        _expect_offerings(clients, [("c6i.large", "us-east-1a")], types=self._TYPES)

        with pytest.raises(LookupError) as excinfo:
            handler.lambda_handler(_subnet_event("Create"))

        message = str(excinfo.value)
        assert message.startswith(
            "no Availability Zone of node group gco-compute offers every instance type "
            "(c6i.large, t3.medium)"
        )
        assert "us-east-1a lacks t3.medium; us-east-1b lacks c6i.large, t3.medium" in message

    def test_a_subnet_ec2_does_not_describe_fails_the_resource(
        self, handler: ModuleType, clients: _Clients
    ) -> None:
        described = {k: v for k, v in _SUBNETS.items() if k != "subnet-0e"}
        _expect_describe_subnets(clients, described)

        with pytest.raises(LookupError, match=r"did not describe subnet\(s\) subnet-0e"):
            handler.lambda_handler(_subnet_event("Create"))

    def test_properties_are_validated_by_name(self, handler: ModuleType) -> None:
        with pytest.raises(ValueError, match=r"SubnetIds must be a list"):
            handler.lambda_handler(_subnet_event("Create", SubnetIds="subnet-0a"))
        with pytest.raises(ValueError, match=r"SubnetIds must name at least one value"):
            handler.lambda_handler(_subnet_event("Create", SubnetIds=[" ", ""]))
        with pytest.raises(ValueError, match=r"InstanceTypes must name at least one value"):
            handler.lambda_handler(_subnet_event("Create", InstanceTypes=[]))
        with pytest.raises(ValueError, match=r"Region is required"):
            handler.lambda_handler(_subnet_event("Create", Region=" "))

    def test_the_node_group_label_defaults_and_types_are_deduplicated(
        self, handler: ModuleType, clients: _Clients
    ) -> None:
        _expect_describe_subnets(clients, _SUBNETS)
        _expect_offerings(
            clients,
            [("c6i.large", zone) for zone in _SUBNETS.values()],
            types=["c6i.large"],
        )

        response = handler.lambda_handler(
            _subnet_event("Create", NodeGroup="", InstanceTypes=["c6i.large", " c6i.large ", ""])
        )

        assert response["PhysicalResourceId"] == "gco-pcs-subnets-node-group"
        assert response["Data"]["SubnetIds"] == list(_SUBNETS)

    def test_delete_is_a_no_op(self, handler: ModuleType) -> None:
        assert handler.lambda_handler(
            {**_subnet_event("Delete"), "PhysicalResourceId": "gco-pcs-subnets-gco-compute"}
        ) == {"PhysicalResourceId": "gco-pcs-subnets-gco-compute"}

    def test_ec2_errors_propagate(self, handler: ModuleType, clients: _Clients) -> None:
        clients.ec2_stub.add_client_error(
            "describe_subnets",
            service_error_code="UnauthorizedOperation",
            expected_params={"SubnetIds": list(_SUBNETS)},
        )

        with pytest.raises(handler.ClientError):
            handler.lambda_handler(_subnet_event("Create"))

    def test_odd_documents_are_skipped_by_the_helpers(self, handler: ModuleType) -> None:
        class _Pages:
            def __init__(self, pages: list[dict[str, Any]]) -> None:
                self.pages = pages

            def paginate(self, **_kwargs: Any) -> list[dict[str, Any]]:
                return self.pages

        class _Ec2:
            def __init__(self, pages: dict[str, list[dict[str, Any]]]) -> None:
                self.pages = pages

            def get_paginator(self, name: str) -> _Pages:
                return _Pages(self.pages[name])

        ec2 = _Ec2(
            {
                "describe_subnets": [
                    {"Subnets": [{"SubnetId": "subnet-0a", "AvailabilityZone": "us-east-1a"}]},
                    {"Subnets": [{"SubnetId": "", "AvailabilityZone": "us-east-1b"}, {}]},
                    {},
                ],
                "describe_instance_type_offerings": [
                    {"InstanceTypeOfferings": [{"InstanceType": "c6i.large", "Location": ""}]},
                    {"InstanceTypeOfferings": [{"Location": "us-east-1a"}]},
                    {},
                ],
            }
        )
        assert handler.subnet_zones(ec2, ["subnet-0a"]) == {"subnet-0a": "us-east-1a"}
        assert handler.offered_types_by_zone(ec2, ["c6i.large"]) == {}
