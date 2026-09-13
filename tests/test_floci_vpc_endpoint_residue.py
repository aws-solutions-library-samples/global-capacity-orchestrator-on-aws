"""Floci layer: deleted-VPC-endpoint acceptance against real EC2 answers.

``scripts/live_release_validation/ownership/vpc_endpoints.py`` decides whether a
Tagging-API record of a gateway endpoint is stale (the index lags deletion by
minutes) or genuine residue, and it defers that decision to EC2:
``DescribeVpcEndpoints`` answering ``InvalidVpcEndpointId.NotFound`` — or a
terminal state — is the only thing that lets a record be stripped. The unit
tests fabricate that ``ClientError``; a typo in the tolerated error code would
pass them and fail closed in the field, turning every clean teardown red the
way the 2026-09-12 run was.

Here the endpoint is real: a VPC and a gateway endpoint are created in the
emulator, the record is checked while the endpoint is ``available`` (kept),
the endpoint is deleted, and the same record is checked again against EC2's
genuine not-found response (stripped, with evidence).
"""

from __future__ import annotations

from contextlib import suppress
from types import SimpleNamespace

import boto3
import pytest

from tests._floci import floci_test_markers

pytestmark = floci_test_markers()

REGION = "us-east-1"


@pytest.fixture()
def gateway_endpoint(verified_floci_endpoint, floci_account):
    """A real S3 gateway endpoint in a throwaway VPC; both removed afterwards."""
    ec2 = boto3.client("ec2", region_name=REGION)
    vpc_id = ec2.create_vpc(CidrBlock="10.77.0.0/16")["Vpc"]["VpcId"]
    route_tables = ec2.describe_route_tables(Filters=[{"Name": "vpc-id", "Values": [vpc_id]}])[
        "RouteTables"
    ]
    endpoint = ec2.create_vpc_endpoint(
        VpcId=vpc_id,
        ServiceName=f"com.amazonaws.{REGION}.s3",
        VpcEndpointType="Gateway",
        RouteTableIds=[table["RouteTableId"] for table in route_tables][:1],
    )["VpcEndpoint"]
    endpoint_id = endpoint["VpcEndpointId"]
    yield {
        "ec2": ec2,
        "vpc_id": vpc_id,
        "endpoint_id": endpoint_id,
        "arn": f"arn:aws:ec2:{REGION}:{floci_account}:vpc-endpoint/{endpoint_id}",
    }
    # The test may already have deleted the endpoint; nothing here depends on
    # how EC2 answers the repeat, only on the VPC going away afterwards.
    with suppress(ec2.exceptions.ClientError):
        ec2.delete_vpc_endpoints(VpcEndpointIds=[endpoint_id])
    ec2.delete_vpc(VpcId=vpc_id)


def _context(account: str) -> SimpleNamespace:
    """The slice of ``RunContext`` the acceptance reads, over a real session."""
    return SimpleNamespace(
        session=boto3.Session(), settings=SimpleNamespace(expected_account=account)
    )


def _inventory(*entries: dict) -> dict:
    return {"regional": {REGION: {"tagged_resources": list(entries), "vpcs": []}}}


def _tagged(arn: str) -> dict:
    return {
        "arn": arn,
        "tags": {
            "aws:cloudformation:stack-name": "gco-live-us-east-1",
            "aws:cloudformation:logical-id": "GCOVpcVpcEndpoints30B02CDE2",
        },
    }


class TestDeletedEndpointAcceptance:
    def test_a_live_endpoint_is_kept_and_a_deleted_one_is_stripped_with_evidence(
        self, gateway_endpoint, floci_account
    ):
        from scripts.live_release_validation.ownership.vpc_endpoints import (
            _strip_deleted_vpc_endpoints,
        )

        ctx = _context(floci_account)
        entry = _tagged(gateway_endpoint["arn"])

        kept, accepted = _strip_deleted_vpc_endpoints(ctx, _inventory(entry))
        assert accepted == []
        assert kept["regional"][REGION]["tagged_resources"] == [entry], (
            "an endpoint EC2 still reports as available is genuine residue"
        )

        gateway_endpoint["ec2"].delete_vpc_endpoints(
            VpcEndpointIds=[gateway_endpoint["endpoint_id"]]
        )

        stripped, accepted = _strip_deleted_vpc_endpoints(ctx, _inventory(entry))
        assert stripped == {"regional": {}}, (
            "with its only record accepted the Region must drop out of the inventory"
        )
        assert accepted == [
            {
                "region": REGION,
                "arn": gateway_endpoint["arn"],
                "endpoint_id": gateway_endpoint["endpoint_id"],
                "endpoint_state": "ABSENT",
                "authority": "ec2:DescribeVpcEndpoints",
                "tags": entry["tags"],
                "note": (
                    "the Resource Groups Tagging API index lags endpoint deletion; "
                    "EC2 is the authority for existence"
                ),
            }
        ]

    def test_an_endpoint_ec2_never_issued_is_absent(self, verified_floci_endpoint, floci_account):
        """The tolerated error code is EC2's real one, not a transcription."""
        from scripts.live_release_validation.ownership.vpc_endpoints import (
            _strip_deleted_vpc_endpoints,
        )

        ghost = f"arn:aws:ec2:{REGION}:{floci_account}:vpc-endpoint/vpce-0123456789abcdef0"
        stripped, accepted = _strip_deleted_vpc_endpoints(
            _context(floci_account), _inventory(_tagged(ghost))
        )
        assert stripped == {"regional": {}}
        assert [item["endpoint_state"] for item in accepted] == ["ABSENT"]

    def test_records_outside_the_run_identity_are_never_consulted(
        self, gateway_endpoint, floci_account
    ):
        """A foreign account's or another Region's ARN stays put without asking EC2.

        These are not this run's to explain away; and asking EC2 in the wrong
        Region about them would answer not-found and wrongly strip them.
        """
        from scripts.live_release_validation.ownership.vpc_endpoints import (
            _strip_deleted_vpc_endpoints,
        )

        gateway_endpoint["ec2"].delete_vpc_endpoints(
            VpcEndpointIds=[gateway_endpoint["endpoint_id"]]
        )
        foreign_account = _tagged(
            gateway_endpoint["arn"].replace(f":{floci_account}:", ":111122223333:")
        )
        other_region = _tagged(gateway_endpoint["arn"].replace(f":{REGION}:", ":eu-west-1:"))
        not_an_endpoint = _tagged(
            f"arn:aws:ec2:{REGION}:{floci_account}:vpc/{gateway_endpoint['vpc_id']}"
        )
        inventory = _inventory(foreign_account, other_region, not_an_endpoint)

        kept, accepted = _strip_deleted_vpc_endpoints(_context(floci_account), inventory)

        assert accepted == []
        assert kept == inventory
