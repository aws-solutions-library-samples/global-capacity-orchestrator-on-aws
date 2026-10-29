"""Tests for ``cli/pcs_preflight.py``, the AWS PCS preflight behind ``gco stacks pcs preflight``.

Real botocore clients (PCS, EC2, EKS, Secrets Manager, IAM) run behind
``Stubber`` so every call and its parameters are asserted without a network.
The block under test comes from ``validate_pcs_config`` so the checks see
exactly what the regional stack would.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import boto3
import pytest
from botocore.exceptions import EndpointConnectionError
from botocore.stub import Stubber

from cli import pcs_preflight as pf
from gco.pcs_config import validate_pcs_config

_REGION = "us-east-1"
_PROJECT = "gco-test"
_EKS_NAME = f"{_PROJECT}-{_REGION}"
_CLUSTER_ID = "pcs_0a1b2c3d4e"
_SECRET_ARN = f"arn:aws:secretsmanager:{_REGION}:123456789012:secret:pcs-jwt-AbCdEf"
_NOW = datetime(2026, 10, 1, tzinfo=UTC)


class _Clients:
    def __init__(self) -> None:
        kwargs = {
            "region_name": _REGION,
            "aws_access_key_id": "testing",
            "aws_secret_access_key": "testing",  # nosec B106  # fake credentials for stubbed clients
        }
        self.clients = {
            service: boto3.client(service, **kwargs)
            for service in ("pcs", "ec2", "eks", "secretsmanager", "iam")
        }
        self.stubs = {service: Stubber(client) for service, client in self.clients.items()}
        self.requested: list[str] = []

    def factory(self, service: str, *, region_name: str) -> Any:
        assert region_name == _REGION
        self.requested.append(service)
        return self.clients[service]

    def __getitem__(self, service: str) -> Stubber:
        return self.stubs[service]


@pytest.fixture
def clients() -> Iterator[_Clients]:
    stubbed = _Clients()
    for stub in stubbed.stubs.values():
        stub.activate()
    yield stubbed
    for stub in stubbed.stubs.values():
        stub.assert_no_pending_responses()
        stub.deactivate()


def _existing(**overrides: Any) -> dict[str, Any]:
    return validate_pcs_config(
        {"enabled": True, "mode": "existing", "cluster_id": _CLUSTER_ID, **overrides}
    )


def _pcs_cluster(**overrides: Any) -> dict[str, Any]:
    cluster: dict[str, Any] = {
        "name": "hpc",
        "id": _CLUSTER_ID,
        "arn": f"arn:aws:pcs:{_REGION}:123456789012:cluster/{_CLUSTER_ID}",
        "status": "ACTIVE",
        "createdAt": _NOW,
        "modifiedAt": _NOW,
        "scheduler": {"type": "SLURM", "version": "25.11"},
        "size": "SMALL",
        "networking": {"subnetIds": ["subnet-0aaa"], "securityGroupIds": ["sg-0pcs"]},
        "endpoints": [
            {"type": "SLURMCTLD", "privateIpAddress": "10.0.1.5", "port": "6817"},
            {"type": "SLURMRESTD", "privateIpAddress": "10.0.1.5", "port": "6820"},
        ],
        "slurmConfiguration": {
            "slurmRest": {"mode": "STANDARD"},
            "jwtAuth": {"jwtKey": {"secretArn": _SECRET_ARN, "secretVersion": "v1"}},
        },
    }
    cluster.update(overrides)
    return cluster


def _expect_get_cluster(clients: _Clients, cluster: dict[str, Any] | None) -> None:
    if cluster is None:
        clients["pcs"].add_client_error(
            "get_cluster",
            service_error_code="ResourceNotFoundException",
            expected_params={"clusterIdentifier": _CLUSTER_ID},
        )
        return
    clients["pcs"].add_response(
        "get_cluster", {"cluster": cluster}, {"clusterIdentifier": _CLUSTER_ID}
    )


def _expect_secret(clients: _Clients) -> None:
    clients["secretsmanager"].add_response(
        "describe_secret", {"ARN": _SECRET_ARN, "Name": "pcs-jwt"}, {"SecretId": _SECRET_ARN}
    )


def _expect_eks(clients: _Clients, *, vpc: str | None = "vpc-0eks", sg: str = "sg-0eks") -> None:
    if vpc is None:
        clients["eks"].add_client_error(
            "describe_cluster",
            service_error_code="ResourceNotFoundException",
            expected_params={"name": _EKS_NAME},
        )
        return
    clients["eks"].add_response(
        "describe_cluster",
        {
            "cluster": {
                "name": _EKS_NAME,
                "resourcesVpcConfig": {"vpcId": vpc, "clusterSecurityGroupId": sg},
            }
        },
        {"name": _EKS_NAME},
    )


def _expect_subnet(clients: _Clients, vpc: str) -> None:
    clients["ec2"].add_response(
        "describe_subnets",
        {"Subnets": [{"SubnetId": "subnet-0aaa", "VpcId": vpc}]},
        {"SubnetIds": ["subnet-0aaa"]},
    )


def _expect_security_group(clients: _Clients, permissions: list[dict[str, Any]]) -> None:
    clients["ec2"].add_response(
        "describe_security_groups",
        {"SecurityGroups": [{"GroupId": "sg-0pcs", "IpPermissions": permissions}]},
        {"GroupIds": ["sg-0pcs"]},
    )


def _expect_vpc_cidrs(clients: _Clients, vpc: str, cidrs: list[str]) -> None:
    clients["ec2"].add_response(
        "describe_vpcs",
        {"Vpcs": [{"VpcId": vpc, "CidrBlockAssociationSet": [{"CidrBlock": c} for c in cidrs]}]},
        {"VpcIds": [vpc]},
    )


def _by_name(report: pf.PreflightReport) -> dict[str, list[pf.Check]]:
    grouped: dict[str, list[pf.Check]] = {}
    for check in report.checks:
        grouped.setdefault(check.name, []).append(check)
    return grouped


def _statuses(report: pf.PreflightReport) -> dict[str, str]:
    """``name -> status`` for checks that appear once."""
    return {check.name: check.status for check in report.checks}


# ---------------------------------------------------------------------------
# cdk.json
# ---------------------------------------------------------------------------


class TestCdkJson:
    def test_block_with_region_overlay_and_fsx(self, tmp_path: Path) -> None:
        cdk = tmp_path / "cdk.json"
        cdk.write_text(
            json.dumps(
                {
                    "context": {
                        "project_name": "acme",
                        "fsx_lustre": {"enabled": False},
                        "fsx_lustre_regions": {"us-east-1": {"enabled": True}},
                        "pcs": {"enabled": False, "managed": {"storage": {"scratch": "fsx"}}},
                        "pcs_regions": {
                            "us-east-1": {
                                "enabled": True,
                                "managed": {"login": {"enabled": False}},
                            }
                        },
                    }
                }
            )
        )
        config = pf.load_pcs_config("us-east-1", cdk_json_path=cdk)
        assert config["enabled"] is True
        # The overlay merges into the base block instead of replacing it.
        assert config["managed"]["storage"]["scratch"] == "fsx"
        assert config["managed"]["login"]["enabled"] is False
        assert pf.project_name(cdk) == "acme"
        # Another Region has no overlay and no FSx: the fsx scratch is refused.
        with pytest.raises(pf.PcsPreflightError, match=r"invalid for eu-west-1: .*fsx_lustre"):
            pf.load_pcs_config("eu-west-1", cdk_json_path=cdk)

    def test_force_enabled_and_defaults(self, tmp_path: Path) -> None:
        cdk = tmp_path / "cdk.json"
        cdk.write_text(json.dumps({"context": {}}))
        config = pf.load_pcs_config("us-east-1", cdk_json_path=cdk)
        assert config["enabled"] is False
        assert pf.load_pcs_config("us-east-1", cdk_json_path=cdk, force_enabled=True)["enabled"]
        assert pf.project_name(cdk) == "gco"
        # Odd shapes read as absent.
        cdk.write_text(json.dumps({"context": {"pcs": "yes", "pcs_regions": ["us-east-1"]}}))
        assert pf.raw_pcs_config(pf._cdk_context(cdk), "us-east-1") == {}
        cdk.write_text(json.dumps(["not", "an", "object"]))
        assert pf._cdk_context(cdk) == {}

    def test_missing_or_broken_cdk_json(self, tmp_path: Path) -> None:
        with pytest.raises(pf.PcsPreflightError, match=r"cdk\.json not found"):
            pf.load_pcs_config("us-east-1", cdk_json_path=tmp_path / "cdk.json")
        broken = tmp_path / "cdk.json"
        broken.write_text("{")
        with pytest.raises(pf.PcsPreflightError, match="not valid JSON"):
            pf.load_pcs_config("us-east-1", cdk_json_path=broken)

    def test_default_path_is_the_working_directory(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        (tmp_path / "cdk.json").write_text(json.dumps({"context": {"project_name": "p"}}))
        monkeypatch.chdir(tmp_path)
        assert pf.project_name() == "p"


class TestChecker:
    def test_a_transport_error_is_a_warning_too(self) -> None:
        """Stubber can only raise ClientError; a BotoCoreError is guarded the same way."""
        checker = pf._Checker(_REGION, lambda *_a, **_k: None)

        def unreachable() -> None:
            raise EndpointConnectionError(endpoint_url="https://pcs.us-east-1.amazonaws.com")

        assert checker.guarded("cluster", unreachable) is None
        assert checker.checks == [
            pf.Check(
                "cluster",
                pf.WARN,
                "could not check: EndpointConnectionError: Could not connect to the endpoint "
                'URL: "https://pcs.us-east-1.amazonaws.com"',
            )
        ]


# ---------------------------------------------------------------------------
# Existing mode
# ---------------------------------------------------------------------------


class TestExistingMode:
    def test_everything_in_order(self, clients: _Clients) -> None:
        _expect_get_cluster(clients, _pcs_cluster())
        _expect_secret(clients)
        _expect_eks(clients)
        _expect_subnet(clients, "vpc-0eks")
        _expect_security_group(
            clients,
            [
                {
                    "IpProtocol": "tcp",
                    "FromPort": 6820,
                    "ToPort": 6820,
                    "UserIdGroupPairs": [{"GroupId": "sg-0eks"}],
                }
            ],
        )
        _expect_vpc_cidrs(clients, "vpc-0eks", ["10.0.0.0/16"])

        report = pf.run_preflight(
            _REGION, _existing(), project=_PROJECT, client_factory=clients.factory
        )

        assert report.ok
        assert (report.mode, report.enabled, report.cluster) == ("existing", True, _CLUSTER_ID)
        assert _statuses(report) == {
            "cluster": pf.PASS,
            "cluster-status": pf.PASS,
            "slurm-version": pf.PASS,
            "rest-api": pf.PASS,
            "rest-endpoint": pf.PASS,
            "jwt-key": pf.PASS,
            "network": pf.PASS,
            "security-group": pf.PASS,
        }
        details = {c.name: c.detail for c in report.checks}
        assert details["cluster"] == f"{_CLUSTER_ID} (hpc) found"
        assert details["rest-endpoint"] == "SLURMRESTD at 10.0.1.5:6820"
        assert details["jwt-key"] == "signing key secret pcs-jwt-AbCdEf"
        assert details["network"] == "cluster VPC vpc-0eks is the regional VPC"
        assert details["security-group"] == "sg-0pcs admits tcp/6820 from sg-0eks"
        assert clients.requested == ["pcs", "secretsmanager", "eks", "ec2"]
        document = report.to_dict()
        assert document["ok"] is True and document["checks"][0]["name"] == "cluster"

    def test_a_missing_cluster_fails_and_stops(self, clients: _Clients) -> None:
        _expect_get_cluster(clients, None)
        report = pf.run_preflight(
            _REGION, _existing(), project=_PROJECT, client_factory=clients.factory
        )
        assert [(c.name, c.status) for c in report.checks] == [("cluster", pf.FAIL)]
        assert "does not exist in us-east-1" in report.checks[0].detail
        assert not report.ok

    def test_a_denied_get_cluster_warns_and_stops(self, clients: _Clients) -> None:
        clients["pcs"].add_client_error(
            "get_cluster",
            service_error_code="AccessDeniedException",
            service_message="not allowed",
            expected_params={"clusterIdentifier": _CLUSTER_ID},
        )
        report = pf.run_preflight(
            _REGION, _existing(), project=_PROJECT, client_factory=clients.factory
        )
        assert [(c.name, c.status) for c in report.checks] == [("cluster", pf.WARN)]
        assert "could not check: AccessDeniedException: not allowed" in report.checks[0].detail
        assert report.ok  # warnings never block

    def test_a_cluster_the_api_cannot_use(self, clients: _Clients) -> None:
        _expect_get_cluster(
            clients,
            _pcs_cluster(
                status="CREATING",
                scheduler={"type": "SLURM", "version": "25.05"},
                endpoints=[],
                slurmConfiguration={"slurmRest": {"mode": "NONE"}},
            ),
        )
        _expect_eks(clients, vpc=None)
        _expect_subnet(clients, "vpc-0other")

        report = pf.run_preflight(
            _REGION,
            _existing(endpoint_cidr="10.42.0.0/16"),
            project=_PROJECT,
            client_factory=clients.factory,
        )

        statuses = _statuses(report)
        assert statuses["cluster-status"] == pf.FAIL
        assert statuses["slurm-version"] == pf.FAIL
        assert statuses["rest-api"] == pf.FAIL
        assert statuses["rest-endpoint"] == pf.SKIP
        assert statuses["jwt-key"] == pf.FAIL
        assert statuses["network"] == pf.FAIL
        assert statuses["security-group"] == pf.SKIP
        details = {c.name: c.detail for c in report.checks}
        assert "the API needs ACTIVE" in details["cluster-status"]
        assert "served by Slurm 25.11 or 26.05" in details["slurm-version"]
        assert "slurmRest={mode=STANDARD}" in details["rest-api"]
        assert details["rest-endpoint"].endswith("(the REST API is off)")
        assert details["jwt-key"] == "GetCluster reports no JWT signing key secret"
        assert "does not cover the REST endpoint (unknown)" in details["network"]
        assert "is not deployed yet" in details["security-group"]
        assert len(report.failures) == 5

    def test_a_peered_cluster_reached_by_cidr(self, clients: _Clients) -> None:
        _expect_get_cluster(clients, _pcs_cluster())
        _expect_secret(clients)
        _expect_eks(clients)
        _expect_subnet(clients, "vpc-0peer")
        # No group rule, but a CIDR rule covering the whole regional VPC.
        _expect_security_group(
            clients,
            [
                {
                    "IpProtocol": "udp",
                    "FromPort": 6820,
                    "ToPort": 6820,
                    "IpRanges": [{"CidrIp": "0.0.0.0/0"}],
                },
                {"IpProtocol": "-1", "IpRanges": [{"CidrIp": "10.0.0.0/8"}, {"CidrIp": "bogus"}]},
            ],
        )
        _expect_vpc_cidrs(clients, "vpc-0eks", ["10.0.0.0/16", "2600:1f18::/56"])

        report = pf.run_preflight(
            _REGION,
            _existing(endpoint_cidr="10.0.1.0/24"),
            project=_PROJECT,
            client_factory=clients.factory,
        )

        assert report.ok
        details = {c.name: c.detail for c in report.checks}
        assert details["network"] == (
            "pcs.endpoint_cidr 10.0.1.0/24 covers the REST endpoint 10.0.1.5 "
            "(cluster VPC vpc-0peer, regional VPC vpc-0eks; the VPCs must be peered)"
        )
        assert details["security-group"] == "sg-0pcs admits tcp/6820 from sg-0eks"

    def test_before_the_first_deploy_network_checks_are_skipped(self, clients: _Clients) -> None:
        _expect_get_cluster(clients, _pcs_cluster())
        _expect_secret(clients)
        _expect_eks(clients, vpc=None)
        _expect_subnet(clients, "vpc-0eks")

        report = pf.run_preflight(
            _REGION, _existing(), project=_PROJECT, client_factory=clients.factory
        )

        assert report.ok
        statuses = _statuses(report)
        assert statuses["network"] == pf.SKIP and statuses["security-group"] == pf.SKIP
        assert "(it is in vpc-0eks)" in {c.name: c.detail for c in report.checks}["network"]

    def test_wrong_vpc_and_a_group_without_the_rule(self, clients: _Clients) -> None:
        _expect_get_cluster(clients, _pcs_cluster())
        _expect_secret(clients)
        _expect_eks(clients)
        _expect_subnet(clients, "vpc-0other")
        _expect_security_group(
            clients,
            [
                {
                    "IpProtocol": "tcp",
                    "FromPort": 22,
                    "ToPort": 22,
                    "UserIdGroupPairs": [{"GroupId": "sg-0eks"}],
                },
                {"IpProtocol": "-1", "UserIdGroupPairs": [{"GroupId": "sg-0pcs"}]},
                {
                    "IpProtocol": "tcp",
                    "FromPort": 6820,
                    "ToPort": 6820,
                    "IpRanges": [{"CidrIp": "192.168.0.0/16"}],
                },
            ],
        )
        _expect_vpc_cidrs(clients, "vpc-0eks", ["10.0.0.0/16"])

        report = pf.run_preflight(
            _REGION, _existing(), project=_PROJECT, client_factory=clients.factory
        )

        statuses = _statuses(report)
        assert statuses["network"] == pf.FAIL and statuses["security-group"] == pf.FAIL
        details = {c.name: c.detail for c in report.checks}
        assert "is not the regional VPC vpc-0eks; move the cluster" in details["network"]
        assert (
            "does not admit tcp/6820 from the EKS cluster security group sg-0eks"
            in (details["security-group"])
        )

    def test_lookup_failures_are_warnings(self, clients: _Clients) -> None:
        _expect_get_cluster(clients, _pcs_cluster())
        clients["secretsmanager"].add_client_error(
            "describe_secret",
            service_error_code="AccessDeniedException",
            expected_params={"SecretId": _SECRET_ARN},
        )
        clients["eks"].add_client_error(
            "describe_cluster",
            service_error_code="AccessDeniedException",
            expected_params={"name": _EKS_NAME},
        )
        clients["ec2"].add_client_error(
            "describe_subnets",
            service_error_code="UnauthorizedOperation",
            expected_params={"SubnetIds": ["subnet-0aaa"]},
        )

        report = pf.run_preflight(
            _REGION, _existing(), project=_PROJECT, client_factory=clients.factory
        )

        grouped = _by_name(report)
        assert [c.status for c in grouped["jwt-key"]] == [pf.WARN]
        assert [c.status for c in grouped["eks-cluster"]] == [pf.WARN]
        assert [c.status for c in grouped["network"]] == [pf.WARN]
        # Without the EKS cluster the security-group check is skipped.
        assert [c.status for c in grouped["security-group"]] == [pf.SKIP]
        assert report.ok

    def test_security_group_lookup_failure_is_a_warning(self, clients: _Clients) -> None:
        # GetCluster without subnets or security groups: nothing to describe
        # for the network check, and the VPC CIDR lookup is denied.
        _expect_get_cluster(clients, _pcs_cluster(networking={}))
        _expect_secret(clients)
        _expect_eks(clients)
        clients["ec2"].add_client_error(
            "describe_vpcs",
            service_error_code="UnauthorizedOperation",
            expected_params={"VpcIds": ["vpc-0eks"]},
        )

        report = pf.run_preflight(
            _REGION, _existing(), project=_PROJECT, client_factory=clients.factory
        )

        details = {c.name: (c.status, c.detail) for c in report.checks}
        # No subnet: the VPC is unknown, so it cannot be the regional one.
        assert details["network"][0] == pf.FAIL and "(unknown)" in details["network"][1]
        assert details["security-group"] == (pf.WARN, "could not check: UnauthorizedOperation")
        assert clients.requested == ["pcs", "secretsmanager", "eks", "ec2"]

    def test_no_security_group_at_all(self, clients: _Clients) -> None:
        _expect_get_cluster(
            clients, _pcs_cluster(networking={"subnetIds": ["subnet-0aaa"], "securityGroupIds": []})
        )
        _expect_secret(clients)
        _expect_eks(clients)
        _expect_subnet(clients, "vpc-0eks")
        _expect_vpc_cidrs(clients, "vpc-0eks", ["10.0.0.0/16"])

        report = pf.run_preflight(
            _REGION, _existing(), project=_PROJECT, client_factory=clients.factory
        )

        [check] = _by_name(report)["security-group"]
        assert check.status == pf.FAIL and check.detail.startswith(
            "no security group does not admit"
        )


class TestRuleAdmits:
    @pytest.mark.parametrize(
        ("rule", "admits"),
        [
            ({"IpProtocol": "-1", "UserIdGroupPairs": [{"GroupId": "sg-eks"}]}, True),
            (
                {
                    "IpProtocol": "tcp",
                    "FromPort": 6000,
                    "ToPort": 7000,
                    "UserIdGroupPairs": [{"GroupId": "sg-eks"}],
                },
                True,
            ),
            (
                {
                    "IpProtocol": "tcp",
                    "FromPort": 6821,
                    "ToPort": 6821,
                    "UserIdGroupPairs": [{"GroupId": "sg-eks"}],
                },
                False,
            ),
            (
                {
                    "IpProtocol": "udp",
                    "FromPort": 6820,
                    "ToPort": 6820,
                    "UserIdGroupPairs": [{"GroupId": "sg-eks"}],
                },
                False,
            ),
            (
                {
                    "IpProtocol": "tcp",
                    "FromPort": 6820,
                    "ToPort": 6820,
                    "UserIdGroupPairs": [{"GroupId": "sg-other"}],
                },
                False,
            ),
            (
                {
                    "IpProtocol": "tcp",
                    "FromPort": 6820,
                    "ToPort": 6820,
                    "IpRanges": [{"CidrIp": "0.0.0.0/0"}],
                },
                True,
            ),
            (
                {
                    "IpProtocol": "tcp",
                    "FromPort": 6820,
                    "ToPort": 6820,
                    "IpRanges": [{"CidrIp": "10.0.0.0/16"}],
                },
                True,
            ),
            (
                {
                    "IpProtocol": "tcp",
                    "FromPort": 6820,
                    "ToPort": 6820,
                    "IpRanges": [{"CidrIp": "10.0.1.0/24"}],
                },
                False,
            ),
            (
                {
                    "IpProtocol": "tcp",
                    "FromPort": 6820,
                    "ToPort": 6820,
                    "IpRanges": [{"CidrIp": "::/0"}],
                },
                False,
            ),
            ({"IpProtocol": "tcp"}, False),
        ],
    )
    def test_rules(self, rule: dict[str, Any], admits: bool) -> None:
        assert (
            pf._rule_admits(rule, source_group="sg-eks", vpc_cidrs=["10.0.0.0/16", "fd00::/8"])
            is admits
        )


# ---------------------------------------------------------------------------
# Managed mode
# ---------------------------------------------------------------------------


MANAGED_BLOCK: dict[str, Any] = {
    "enabled": True,
    "mode": "managed",
    "managed": {
        "login": {"instance_type": "t4g.small", "availability_zones": ["us-east-1a"]},
        "node_groups": [
            {"name": "gco-compute", "instance_types": ["c6i.large"]},
            {
                "name": "gpu",
                "instance_types": ["p5.48xlarge"],
                "ami": "dlami",
                "efa": True,
                "efa_interfaces": 4,
                "availability_zones": ["us-east-1a"],
            },
            {
                "name": "hpc",
                "instance_types": ["hpc6a.48xlarge"],
                "availability_zones": ["us-east-1b"],
                "capacity_reservation": {"type": "odcr", "id": "cr-0123456789abcdef0"},
            },
            {"name": "spot", "instance_types": ["m6i.large"], "purchase_option": "SPOT"},
        ],
        "queues": [
            {"name": "gco", "node_groups": ["gco-compute", "spot"], "default": True},
            {"name": "gpu", "node_groups": ["gpu", "hpc"]},
        ],
    },
}


def _instance_type(name: str, arch: str, *, efa: bool = False, cards: int = 1) -> dict[str, Any]:
    return {
        "InstanceType": name,
        "ProcessorInfo": {"SupportedArchitectures": [arch]},
        "NetworkInfo": {"EfaSupported": efa, "MaximumNetworkCards": cards},
    }


def _expect_list_clusters(clients: _Clients, names: list[str]) -> None:
    clients["pcs"].add_response(
        "list_clusters",
        {
            "clusters": [
                {
                    "name": name,
                    "id": f"pcs_{index:010x}",
                    "arn": f"arn:aws:pcs:{_REGION}:123456789012:cluster/pcs_{index}",
                    "status": "ACTIVE",
                    "createdAt": _NOW,
                    "modifiedAt": _NOW,
                }
                for index, name in enumerate(names)
            ]
        },
        {},
    )


def _expect_role(clients: _Clients, present: bool) -> None:
    if not present:
        clients["iam"].add_client_error(
            "get_role",
            service_error_code="NoSuchEntity",
            expected_params={"RoleName": "AWSServiceRoleForEC2Spot"},
        )
        return
    clients["iam"].add_response(
        "get_role",
        {
            "Role": {
                "Path": "/aws-service-role/spot.amazonaws.com/",
                "RoleName": "AWSServiceRoleForEC2Spot",
                "RoleId": "AROAEXAMPLEEXAMPLE12",
                "Arn": "arn:aws:iam::123456789012:role/aws-service-role/spot.amazonaws.com/AWSServiceRoleForEC2Spot",
                "CreateDate": _NOW,
            }
        },
        {"RoleName": "AWSServiceRoleForEC2Spot"},
    )


class TestManagedMode:
    _TYPES = ["c6i.large", "hpc6a.48xlarge", "m6i.large", "p5.48xlarge", "t4g.small"]

    def test_everything_in_order(self, clients: _Clients) -> None:
        _expect_list_clusters(clients, ["other"])
        clients["ec2"].add_response(
            "describe_instance_types",
            {
                "InstanceTypes": [
                    _instance_type("c6i.large", "x86_64"),
                    _instance_type("hpc6a.48xlarge", "x86_64", efa=True, cards=2),
                    _instance_type("m6i.large", "x86_64"),
                    _instance_type("p5.48xlarge", "x86_64", efa=True, cards=32),
                    _instance_type("t4g.small", "arm64"),
                ]
            },
            {"InstanceTypes": self._TYPES},
        )
        clients["ec2"].add_response(
            "describe_instance_type_offerings",
            {
                "InstanceTypeOfferings": [
                    {
                        "InstanceType": "p5.48xlarge",
                        "LocationType": "availability-zone",
                        "Location": "us-east-1a",
                    },
                    {
                        "InstanceType": "hpc6a.48xlarge",
                        "LocationType": "availability-zone",
                        "Location": "us-east-1b",
                    },
                    {
                        "InstanceType": "t4g.small",
                        "LocationType": "availability-zone",
                        "Location": "us-east-1a",
                    },
                ]
            },
            {
                "LocationType": "availability-zone",
                "Filters": [
                    {
                        "Name": "instance-type",
                        "Values": ["hpc6a.48xlarge", "p5.48xlarge", "t4g.small"],
                    }
                ],
            },
        )
        clients["ec2"].add_response(
            "describe_capacity_reservations",
            {
                "CapacityReservations": [
                    {
                        "CapacityReservationId": "cr-0123456789abcdef0",
                        "InstanceType": "hpc6a.48xlarge",
                        "AvailabilityZone": "us-east-1b",
                        "State": "active",
                        "ReservationType": "default",
                        "InstanceMatchCriteria": "targeted",
                    }
                ]
            },
            {"CapacityReservationIds": ["cr-0123456789abcdef0"]},
        )
        _expect_role(clients, True)

        report = pf.run_preflight(
            _REGION,
            validate_pcs_config(MANAGED_BLOCK),
            project=_PROJECT,
            client_factory=clients.factory,
        )

        assert report.ok, [c for c in report.checks if c.status != pf.PASS]
        assert (report.mode, report.cluster) == ("managed", "gco-test-pcs")
        grouped = _by_name(report)
        assert [c.detail for c in grouped["cluster-name"]] == ["gco-test-pcs is free"]
        assert len(grouped["instance-types"]) == 5
        assert grouped["instance-types"][0].detail == "c6i.large (gco-compute) available, x86_64"
        assert [c.detail for c in grouped["efa"]] == [
            "gpu: p5.48xlarge supports EFA on 4 of 32 network card(s)"
        ]
        assert sorted(c.detail for c in grouped["zones"]) == [
            "gpu: p5.48xlarge is offered in us-east-1a",
            "hpc: hpc6a.48xlarge is offered in us-east-1b",
            "login: t4g.small is offered in us-east-1a",
        ]
        assert [c.detail for c in grouped["capacity-reservations"]] == [
            "hpc: cr-0123456789abcdef0 active, hpc6a.48xlarge in us-east-1b"
        ]
        assert [c.detail for c in grouped["spot-role"]] == ["AWSServiceRoleForEC2Spot exists"]
        assert clients.requested == ["pcs", "ec2", "iam"]

    def test_everything_wrong(self, clients: _Clients) -> None:
        _expect_list_clusters(clients, ["gco-test-pcs"])
        # One unknown type fails the batch; the retry goes type by type.
        clients["ec2"].add_client_error(
            "describe_instance_types",
            service_error_code="InvalidInstanceType",
            expected_params={"InstanceTypes": self._TYPES},
        )
        singles = {
            "c6i.large": _instance_type("c6i.large", "arm64"),  # name reads x86_64
            "hpc6a.48xlarge": None,  # unknown in the Region
            "m6i.large": _instance_type("m6i.large", "x86_64"),
            "p5.48xlarge": _instance_type("p5.48xlarge", "x86_64", efa=True, cards=2),
            "t4g.small": _instance_type("t4g.small", "arm64"),
        }
        for name in self._TYPES:
            item = singles[name]
            if item is None:
                clients["ec2"].add_client_error(
                    "describe_instance_types",
                    service_error_code="InvalidInstanceType",
                    expected_params={"InstanceTypes": [name]},
                )
            else:
                clients["ec2"].add_response(
                    "describe_instance_types", {"InstanceTypes": [item]}, {"InstanceTypes": [name]}
                )
        clients["ec2"].add_response(
            "describe_instance_type_offerings",
            {"InstanceTypeOfferings": []},
            {
                "LocationType": "availability-zone",
                "Filters": [
                    {
                        "Name": "instance-type",
                        "Values": ["hpc6a.48xlarge", "p5.48xlarge", "t4g.small"],
                    }
                ],
            },
        )
        clients["ec2"].add_response(
            "describe_capacity_reservations",
            {
                "CapacityReservations": [
                    {
                        "CapacityReservationId": "cr-0123456789abcdef0",
                        "InstanceType": "hpc6a.24xlarge",
                        "AvailabilityZone": "us-east-1c",
                        "State": "expired",
                        "ReservationType": "capacity-block",
                        "InstanceMatchCriteria": "open",
                    }
                ]
            },
            {"CapacityReservationIds": ["cr-0123456789abcdef0"]},
        )
        _expect_role(clients, False)

        report = pf.run_preflight(
            _REGION,
            validate_pcs_config(MANAGED_BLOCK),
            project=_PROJECT,
            client_factory=clients.factory,
        )

        assert not report.ok
        grouped = _by_name(report)
        assert grouped["cluster-name"][0].status == pf.WARN
        assert "already exists (pcs_0000000000)" in grouped["cluster-name"][0].detail
        types = {c.detail.split(" ", 1)[0]: c for c in grouped["instance-types"]}
        assert types["c6i.large"].status == pf.FAIL
        assert "is arm64 but its name reads as x86_64" in types["c6i.large"].detail
        assert types["hpc6a.48xlarge"].status == pf.FAIL
        assert types["hpc6a.48xlarge"].detail.endswith("is not available in us-east-1")
        assert types["m6i.large"].status == pf.PASS
        assert [c.detail for c in grouped["efa"]] == [
            "gpu: p5.48xlarge has 2 network card(s) but efa_interfaces is 4"
        ]
        assert all(c.status == pf.FAIL for c in grouped["zones"])
        assert "hpc: hpc6a.48xlarge is not offered in us-east-1b" in [
            c.detail for c in grouped["zones"]
        ]
        [reservation] = grouped["capacity-reservations"]
        assert reservation.status == pf.FAIL
        assert "state is expired" in reservation.detail
        assert "it reserves hpc6a.24xlarge" in reservation.detail
        assert "it is in us-east-1c" in reservation.detail
        assert "it is a Capacity Block but capacity_reservation.type is odcr" in reservation.detail
        assert "it is an open reservation" in reservation.detail
        [spot] = grouped["spot-role"]
        assert spot.status == pf.FAIL and "create-service-linked-role" in spot.detail

    def test_efa_unsupported_and_reservation_kinds(self, clients: _Clients) -> None:
        block = {
            "enabled": True,
            "managed": {
                "login": {"enabled": False},
                "node_groups": [
                    {
                        "name": "efa",
                        "instance_types": ["c6i.large"],
                        "efa": True,
                        "availability_zones": ["us-east-1a"],
                    },
                    {
                        "name": "block",
                        "instance_types": ["p5.48xlarge"],
                        "availability_zones": ["us-east-1a"],
                        "capacity_reservation": {
                            "type": "capacity_block",
                            "id": "cr-0bbbbbbbbbbbbbbbb",
                        },
                    },
                    {
                        "name": "shared",
                        "instance_types": ["c6i.large"],
                        "availability_zones": ["us-east-1a"],
                        "capacity_reservation": {
                            "type": "interruptible",
                            "id": "cr-0cccccccccccccccc",
                        },
                    },
                    {
                        "name": "gone",
                        "instance_types": ["c6i.large"],
                        "availability_zones": ["us-east-1a"],
                        "capacity_reservation": {"type": "odcr", "id": "cr-0dddddddddddddddd"},
                    },
                    {
                        # An EFA group whose type the Region does not know: the
                        # instance-types check fails it, the efa check has nothing to say.
                        "name": "efa-gone",
                        "instance_types": ["hpc9z.96xlarge"],
                        "efa": True,
                        "availability_zones": ["us-east-1a"],
                    },
                ],
                "queues": [
                    {
                        "name": "gco",
                        "node_groups": ["efa", "block", "shared", "gone", "efa-gone"],
                        "default": True,
                    }
                ],
            },
        }
        types = ["c6i.large", "hpc9z.96xlarge", "p5.48xlarge"]
        _expect_list_clusters(clients, [])
        clients["ec2"].add_client_error(
            "describe_instance_types",
            service_error_code="InvalidInstanceType",
            expected_params={"InstanceTypes": types},
        )
        clients["ec2"].add_response(
            "describe_instance_types",
            {"InstanceTypes": [_instance_type("c6i.large", "x86_64")]},
            {"InstanceTypes": ["c6i.large"]},
        )
        clients["ec2"].add_client_error(
            "describe_instance_types",
            service_error_code="InvalidInstanceType",
            expected_params={"InstanceTypes": ["hpc9z.96xlarge"]},
        )
        clients["ec2"].add_response(
            "describe_instance_types",
            {"InstanceTypes": [_instance_type("p5.48xlarge", "x86_64", efa=True, cards=32)]},
            {"InstanceTypes": ["p5.48xlarge"]},
        )
        clients["ec2"].add_response(
            "describe_instance_type_offerings",
            {
                "InstanceTypeOfferings": [
                    {
                        "InstanceType": "c6i.large",
                        "LocationType": "availability-zone",
                        "Location": "us-east-1a",
                    },
                    {
                        "InstanceType": "p5.48xlarge",
                        "LocationType": "availability-zone",
                        "Location": "us-east-1a",
                    },
                ]
            },
            {
                "LocationType": "availability-zone",
                "Filters": [{"Name": "instance-type", "Values": types}],
            },
        )
        clients["ec2"].add_response(
            "describe_capacity_reservations",
            {
                "CapacityReservations": [
                    {
                        "CapacityReservationId": "cr-0bbbbbbbbbbbbbbbb",
                        "InstanceType": "p5.48xlarge",
                        "AvailabilityZone": "us-east-1a",
                        "State": "scheduled",
                        "ReservationType": "capacity-block",
                        "InstanceMatchCriteria": "targeted",
                    },
                    {
                        "CapacityReservationId": "cr-0cccccccccccccccc",
                        "InstanceType": "c6i.large",
                        "AvailabilityZone": "us-east-1a",
                        "State": "active",
                        "ReservationType": "default",
                        "InstanceMatchCriteria": "targeted",
                        "Interruptible": False,
                    },
                ]
            },
            {
                "CapacityReservationIds": [
                    "cr-0bbbbbbbbbbbbbbbb",
                    "cr-0cccccccccccccccc",
                    "cr-0dddddddddddddddd",
                ]
            },
        )

        report = pf.run_preflight(
            _REGION, validate_pcs_config(block), project=_PROJECT, client_factory=clients.factory
        )

        grouped = _by_name(report)
        assert [c.detail for c in grouped["efa"]] == [
            "efa: c6i.large does not support EFA; drop efa or pick an EFA-capable type"
        ]
        assert "hpc9z.96xlarge (efa-gone) is not available in us-east-1" in [
            c.detail for c in grouped["instance-types"]
        ]
        assert "efa-gone: hpc9z.96xlarge is not offered in us-east-1a" in [
            c.detail for c in grouped["zones"]
        ]
        reservations = {c.detail.split(":", 1)[0]: c for c in grouped["capacity-reservations"]}
        assert reservations["block"].status == pf.PASS  # scheduled is fine for a Capacity Block
        assert reservations["shared"].status == pf.FAIL
        assert "it is not an interruptible reservation" in reservations["shared"].detail
        assert reservations["gone"].status == pf.FAIL
        assert "does not exist in us-east-1" in reservations["gone"].detail
        assert "spot-role" not in grouped and "login" not in str(grouped.get("zones"))

    def test_api_errors_are_warnings(self, clients: _Clients) -> None:
        clients["pcs"].add_client_error(
            "list_clusters", service_error_code="AccessDeniedException", expected_params={}
        )
        clients["ec2"].add_client_error(
            "describe_instance_types",
            service_error_code="UnauthorizedOperation",
            expected_params={"InstanceTypes": self._TYPES},
        )
        clients["ec2"].add_client_error(
            "describe_instance_type_offerings",
            service_error_code="UnauthorizedOperation",
            expected_params={
                "LocationType": "availability-zone",
                "Filters": [
                    {
                        "Name": "instance-type",
                        "Values": ["hpc6a.48xlarge", "p5.48xlarge", "t4g.small"],
                    }
                ],
            },
        )
        clients["ec2"].add_client_error(
            "describe_capacity_reservations",
            service_error_code="UnauthorizedOperation",
            expected_params={"CapacityReservationIds": ["cr-0123456789abcdef0"]},
        )
        clients["iam"].add_client_error(
            "get_role",
            service_error_code="AccessDenied",
            expected_params={"RoleName": "AWSServiceRoleForEC2Spot"},
        )

        report = pf.run_preflight(
            _REGION,
            validate_pcs_config(MANAGED_BLOCK),
            project=_PROJECT,
            client_factory=clients.factory,
        )

        assert report.ok
        assert {c.name: c.status for c in report.checks} == {
            "pcs-service": pf.WARN,
            "instance-types": pf.WARN,
            "zones": pf.WARN,
            "capacity-reservations": pf.WARN,
            "spot-role": pf.WARN,
        }

    def test_a_single_type_lookup_error_other_than_unknown_propagates_as_a_warning(
        self, clients: _Clients
    ) -> None:
        block = {
            "enabled": True,
            "managed": {"login": {"enabled": False}},
        }
        _expect_list_clusters(clients, [])
        clients["ec2"].add_client_error(
            "describe_instance_types",
            service_error_code="InvalidInstanceType",
            expected_params={"InstanceTypes": ["c6i.large"]},
        )
        clients["ec2"].add_client_error(
            "describe_instance_types",
            service_error_code="RequestLimitExceeded",
            expected_params={"InstanceTypes": ["c6i.large"]},
        )

        report = pf.run_preflight(
            _REGION, validate_pcs_config(block), project=_PROJECT, client_factory=clients.factory
        )

        [check] = _by_name(report)["instance-types"]
        assert check.status == pf.WARN and "RequestLimitExceeded" in check.detail
        # No zones, reservations or Spot in the default block.
        assert set(_by_name(report)) == {"pcs-service", "cluster-name", "instance-types"}


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


class TestRendering:
    def test_report_and_summary(self) -> None:
        report = pf.PreflightReport(
            region=_REGION,
            mode="existing",
            enabled=False,
            cluster=_CLUSTER_ID,
            checks=[
                pf.Check("cluster", pf.PASS, "found"),
                pf.Check("rest-api", pf.FAIL, "slurmRest NONE"),
                pf.Check("jwt-key", pf.WARN, "could not check"),
                pf.Check("network", pf.SKIP, "not deployed yet"),
            ],
        )
        text = pf.render_report(report)
        assert (
            "AWS PCS preflight — us-east-1 (existing mode, disabled in cdk.json): pcs_0a1b2c3d4e"
            in text
        )
        assert "  ✓ cluster                found" in text
        assert "  ✗ rest-api               slurmRest NONE" in text
        assert "  ! jwt-key" in text and "  - network" in text
        assert text.rstrip().endswith("1 failure(s)")
        empty = pf.PreflightReport(region=_REGION, mode="managed", enabled=True, cluster="gco-pcs")
        assert "(nothing to check)" in pf.render_report(empty)
        assert pf.render_report(empty).rstrip().endswith("ready: no failures")
        summary = pf.summarize([report, empty])
        assert summary["ok"] is False
        assert [r["region"] for r in summary["regions"]] == [_REGION, _REGION]
        assert summary["regions"][1]["ok"] is True
