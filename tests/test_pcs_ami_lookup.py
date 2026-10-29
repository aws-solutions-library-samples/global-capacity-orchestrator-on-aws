"""Tests for the AWS PCS AMI lookup custom resource handler.

``lambda/pcs-ami-lookup/handler.py`` is the ``cr.Provider`` on-event handler
the managed PCS cluster runs once per distinct ``(ami kind, architecture)`` its
node groups ask for. The contract: Create and Update resolve the newest
AWS-published AMI whose description lists the cluster's Slurm version (the
PCS sample AMI by name search; the PCS-ready DLAMI through its SSM public
parameter first, the name search as the fallback), Delete touches nothing,
and malformed properties fail by name. Real botocore clients run behind
``Stubber`` so every AWS call and its exact parameters are asserted without a
network.
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
    return load_lambda_module("pcs-ami-lookup")


class _Clients:
    """Stubbed EC2 and SSM clients plus the factory calls seen."""

    def __init__(self) -> None:
        self.ec2 = boto3.client(
            "ec2",
            region_name=_REGION,
            aws_access_key_id="testing",
            aws_secret_access_key="testing",  # nosec B106  # fake credentials for a stubbed client
        )
        self.ssm = boto3.client(
            "ssm",
            region_name=_REGION,
            aws_access_key_id="testing",
            aws_secret_access_key="testing",  # nosec B106  # fake credentials for a stubbed client
        )
        self.ec2_stub = Stubber(self.ec2)
        self.ssm_stub = Stubber(self.ssm)
        self.requests: list[tuple[str, dict[str, Any]]] = []

    def factory(self, service: str, **kwargs: Any) -> Any:
        self.requests.append((service, kwargs))
        return {"ec2": self.ec2, "ssm": self.ssm}[service]

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
    with stubbed.ec2_stub, stubbed.ssm_stub:
        yield stubbed
        stubbed.ec2_stub.assert_no_pending_responses()
        stubbed.ssm_stub.assert_no_pending_responses()


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
        assert handler.lambda_handler(_event("Delete")) == {
            "PhysicalResourceId": "gco-pcs-ami-lookup"
        }

    def test_unknown_request_types_are_refused(self, handler: ModuleType) -> None:
        with pytest.raises(ValueError, match=r"Unsupported RequestType: 'Read'"):
            handler.lambda_handler(_event("Read"))

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
