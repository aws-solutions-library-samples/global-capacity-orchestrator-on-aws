"""Floci layer: regional data-plane Lambda handlers against real emulated AWS.

Covered here, each through the production handler module with the session
environment pointing boto3 at the emulator:

* ``lambda/capacity-poller`` — a full ``lambda_handler`` run writing a real
  DynamoDB item. The emulator's EC2 rejects the three capacity probes with
  ``ClientError`` (probed; documented in docs/FLOCI_TESTING.md), which
  exercises the poller's degraded-signal path for real: the snapshot must
  still be written with the spot fields absent and the block counters zero.
* ``lambda/image-lookup`` — adopt-or-create against real ECR repositories.
  ``CreateRepository`` fails under a finch-hosted emulator (documented
  docker-socket gap) but works on the GitHub runners; the fixture skips
  locally and runs fully in CI.
* ``lambda/regional-api-proxy`` — the registry-driven ALB resolution chain
  (``SSM parameter -> DNS shape -> ELBv2 ownership -> Gateway tag``) against
  a real internal ALB, including its fail-closed rejections.
* ``lambda/ga-registration`` — the SSM endpoint-registry half and the
  tag-based ALB discovery half. The Global Accelerator and EKS halves stay
  unit-mocked (both are documented emulator gaps).

The ``AWS_URL_SUFFIX`` seam matters here: emulator ALB DNS names end in
``.elb.localhost.floci.io``, and the proxy validates DNS shape against the
partition suffix from the environment, so these tests configure the suffix
the emulator actually serves — exercising the same code path a real
partition change would.
"""

from __future__ import annotations

import uuid
from decimal import Decimal

import boto3
import pytest
from botocore.exceptions import ClientError

from tests._floci import floci_test_markers, unique_name
from tests._lambda_imports import load_lambda_module

pytestmark = floci_test_markers()

#: DNS suffix the emulator's ELBv2 serves (probed: ``<name>-<hex>.elb.localhost.floci.io``).
_FLOCI_URL_SUFFIX = "localhost.floci.io"


# ---------------------------------------------------------------------------
# capacity-poller
# ---------------------------------------------------------------------------


class TestCapacityPoller:
    @pytest.fixture()
    def history_table(self, verified_floci_endpoint: str):
        dynamodb = boto3.client("dynamodb")
        table_name = unique_name("gco-capacity-history")
        dynamodb.create_table(
            TableName=table_name,
            AttributeDefinitions=[
                {"AttributeName": "pk", "AttributeType": "S"},
                {"AttributeName": "sk", "AttributeType": "S"},
            ],
            KeySchema=[
                {"AttributeName": "pk", "KeyType": "HASH"},
                {"AttributeName": "sk", "KeyType": "RANGE"},
            ],
            BillingMode="PAY_PER_REQUEST",
        )
        dynamodb.get_waiter("table_exists").wait(TableName=table_name)
        return table_name

    def test_poll_writes_degraded_snapshot_when_capacity_apis_reject(
        self, history_table, monkeypatch
    ):
        # The emulator's EC2 rejects get_spot_placement_scores,
        # describe_spot_price_history, and describe_capacity_block_offerings
        # with ClientError — exactly the degradation the poller must absorb.
        monkeypatch.setenv("CAPACITY_HISTORY_TABLE_NAME", history_table)
        monkeypatch.setenv("WATCH_INSTANCE_TYPES", "g4dn.xlarge")
        monkeypatch.setenv("ENABLED_REGIONS", "us-east-1")
        handler = load_lambda_module("capacity-poller")

        result = handler.lambda_handler({}, None)
        assert result["written"] == 1
        assert result["errors"] == 0, (
            "per-signal API failures must degrade the snapshot, not error the poll"
        )

        items = boto3.resource("dynamodb").Table(history_table).scan()["Items"]
        assert len(items) == 1
        item = items[0]
        assert item["pk"] == "g4dn.xlarge#us-east-1"
        assert item["sk"] == item["timestamp"]
        assert isinstance(item["ttl"], Decimal | int)
        assert item["capacity_blocks_available"] == 0
        assert item["capacity_blocks_total"] == 0
        assert item["capacity_blocks_long_available"] == 0
        for absent in ("spot_score", "spot_price", "az_count"):
            assert absent not in item, (
                f"{absent} must be omitted (not zeroed) when its API is unavailable, "
                "so the history store reads it as absent"
            )

    def test_disabling_the_long_probe_omits_the_long_tier(self, history_table, monkeypatch):
        monkeypatch.setenv("CAPACITY_HISTORY_TABLE_NAME", history_table)
        monkeypatch.setenv("WATCH_INSTANCE_TYPES", "g4dn.xlarge")
        monkeypatch.setenv("ENABLED_REGIONS", "us-east-1")
        monkeypatch.setenv("CAPACITY_BLOCK_LONG_DURATION_HOURS", "0")
        handler = load_lambda_module("capacity-poller")

        assert handler.lambda_handler({}, None)["written"] == 1
        (item,) = boto3.resource("dynamodb").Table(history_table).scan()["Items"]
        assert "capacity_blocks_long_available" not in item
        assert "capacity_blocks_long_total" not in item

    def test_missing_table_configuration_is_rejected(self, verified_floci_endpoint, monkeypatch):
        monkeypatch.delenv("CAPACITY_HISTORY_TABLE_NAME", raising=False)
        handler = load_lambda_module("capacity-poller")
        with pytest.raises(ValueError, match="CAPACITY_HISTORY_TABLE_NAME"):
            handler.lambda_handler({}, None)

    def test_empty_watchlist_writes_nothing(self, history_table, monkeypatch):
        monkeypatch.setenv("CAPACITY_HISTORY_TABLE_NAME", history_table)
        monkeypatch.setenv("WATCH_INSTANCE_TYPES", "")
        monkeypatch.setenv("ENABLED_REGIONS", "us-east-1")
        handler = load_lambda_module("capacity-poller")
        result = handler.lambda_handler({}, None)
        assert (result["written"], result["errors"]) == (0, 0)
        assert boto3.resource("dynamodb").Table(history_table).scan()["Items"] == []


# ---------------------------------------------------------------------------
# image-lookup (ECR adopt-or-create custom resource)
# ---------------------------------------------------------------------------


class TestImageLookup:
    @pytest.fixture()
    def ecr(self, verified_floci_endpoint: str):
        """ECR client, skipping when the emulator host cannot create repos.

        ``CreateRepository`` needs the emulator's docker socket; under a local
        finch-hosted container it fails with ``InternalFailure`` (documented in
        docs/FLOCI_TESTING.md) while the GitHub-hosted CI emulator supports it.
        """
        client = boto3.client("ecr")
        canary = unique_name("gco/floci-canary")
        try:
            client.create_repository(repositoryName=canary)
        except ClientError as exc:
            code = exc.response.get("Error", {}).get("Code", "")
            pytest.skip(
                f"emulator host cannot create ECR repositories ({code}); "
                "this suite runs fully on the CI emulator"
            )
        client.delete_repository(repositoryName=canary, force=True)
        return client

    def test_create_adopts_an_existing_repository(self, ecr):
        handler = load_lambda_module("image-lookup")
        name = unique_name("gco/adopted")
        ecr.create_repository(repositoryName=name)

        result = handler.lambda_handler(
            {"RequestType": "Create", "ResourceProperties": {"RepositoryName": name}},
            None,
        )
        assert result["Data"]["Adopted"] == "true", (
            "a retained repository from a prior deploy must be adopted, not fail with "
            "RepositoryAlreadyExists"
        )
        assert result["Data"]["RepositoryName"] == name

    def test_create_builds_a_missing_repository(self, ecr):
        handler = load_lambda_module("image-lookup")
        name = unique_name("gco/created")
        result = handler.lambda_handler(
            {"RequestType": "Create", "ResourceProperties": {"RepositoryName": name}},
            None,
        )
        assert result["Data"]["Adopted"] == "false"
        described = ecr.describe_repositories(repositoryNames=[name])["repositories"]
        assert len(described) == 1, "the repository must actually exist after Create"

    def test_delete_with_retain_policy_preserves_the_repository(self, ecr):
        handler = load_lambda_module("image-lookup")
        name = unique_name("gco/retained")
        ecr.create_repository(repositoryName=name)

        result = handler.lambda_handler(
            {
                "RequestType": "Delete",
                "PhysicalResourceId": name,
                "ResourceProperties": {"RepositoryName": name, "RemovalPolicy": "retain"},
            },
            None,
        )
        assert result["Data"] == {"Deleted": "false", "Reason": "removal-policy-retain"}
        assert ecr.describe_repositories(repositoryNames=[name])["repositories"], (
            "RemovalPolicy=retain must leave the repository in place"
        )

    def test_delete_of_an_absent_repository_succeeds(self, ecr):
        handler = load_lambda_module("image-lookup")
        result = handler.lambda_handler(
            {
                "RequestType": "Delete",
                "PhysicalResourceId": "gone",
                "ResourceProperties": {
                    "RepositoryName": unique_name("gco/never-existed"),
                    "RemovalPolicy": "destroy",
                },
            },
            None,
        )
        assert result["Data"] == {"Deleted": "false"}

    def test_destroy_without_retain_tag_deletes_the_repository(self, ecr):
        handler = load_lambda_module("image-lookup")
        name = unique_name("gco/destroyed")
        created = ecr.create_repository(repositoryName=name)["repository"]
        try:
            ecr.list_tags_for_resource(resourceArn=created["repositoryArn"])
        except ClientError as exc:
            pytest.skip(
                "emulator does not support ECR ListTagsForResource "
                f"({exc.response.get('Error', {}).get('Code', '')})"
            )

        result = handler.lambda_handler(
            {
                "RequestType": "Delete",
                "PhysicalResourceId": name,
                "ResourceProperties": {"RepositoryName": name, "RemovalPolicy": "destroy"},
            },
            None,
        )
        assert result["Data"] == {"Deleted": "true"}
        with pytest.raises(ClientError):
            ecr.describe_repositories(repositoryNames=[name])


# ---------------------------------------------------------------------------
# Shared Gateway-ALB fixture (regional-api-proxy + ga-registration)
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def gateway_alb(verified_floci_endpoint: str):
    """A real internal ALB carrying the exact GCO Gateway ownership tags."""
    project = f"gco{uuid.uuid4().hex[:8]}"
    cluster_name = f"{project}-us-east-1"
    ec2 = boto3.client("ec2")
    elbv2 = boto3.client("elbv2")

    vpc = ec2.create_vpc(CidrBlock="10.42.0.0/16")["Vpc"]["VpcId"]
    subnets = [
        ec2.create_subnet(VpcId=vpc, CidrBlock=cidr, AvailabilityZone=zone)["Subnet"]["SubnetId"]
        for cidr, zone in (("10.42.1.0/24", "us-east-1a"), ("10.42.2.0/24", "us-east-1b"))
    ]
    alb = elbv2.create_load_balancer(
        Name=f"{project}-gw", Subnets=subnets, Scheme="internal", Type="application"
    )["LoadBalancers"][0]
    elbv2.add_tags(
        ResourceArns=[alb["LoadBalancerArn"]],
        Tags=[
            {"Key": "gco.aws/gateway", "Value": "gco-system/gco-gateway"},
            {"Key": "elbv2.k8s.aws/cluster", "Value": cluster_name},
        ],
    )
    return {
        "project": project,
        "cluster_name": cluster_name,
        "dns_name": alb["DNSName"],
        "arn": alb["LoadBalancerArn"],
        "elbv2": elbv2,
    }


# ---------------------------------------------------------------------------
# regional-api-proxy endpoint resolution
# ---------------------------------------------------------------------------


class TestRegionalApiProxyResolution:
    @pytest.fixture()
    def proxy(self, gateway_alb, floci_account, monkeypatch):
        monkeypatch.setenv("REGISTRY_REGION", "us-east-1")
        monkeypatch.setenv("TARGET_REGION", "us-east-1")
        monkeypatch.setenv("PROJECT_NAME", gateway_alb["project"])
        monkeypatch.setenv("AWS_ACCOUNT_ID", floci_account)
        monkeypatch.setenv("AWS_URL_SUFFIX", _FLOCI_URL_SUFFIX)
        monkeypatch.delenv("ALB_ENDPOINT", raising=False)
        # Fresh module per test: the resolution cache starts empty.
        return load_lambda_module("regional-api-proxy", shared_dirs=["proxy-shared"])

    @pytest.fixture()
    def registry_parameter(self, gateway_alb):
        ssm = boto3.client("ssm")
        name = f"/{gateway_alb['project']}/alb-hostname-us-east-1"
        ssm.put_parameter(Name=name, Value=gateway_alb["dns_name"], Type="String", Overwrite=True)
        yield name
        ssm.delete_parameter(Name=name)

    def test_resolves_and_verifies_the_registered_gateway_alb(
        self, proxy, gateway_alb, registry_parameter
    ):
        assert proxy._resolve_registered_endpoint() == gateway_alb["dns_name"], (
            "the full chain — SSM registry, DNS-shape validation, ELBv2 ownership, "
            "Gateway tag — must accept the exactly owned internal ALB"
        )

    def test_missing_registry_parameter_fails_closed(self, proxy):
        with pytest.raises(RuntimeError, match="could not be verified"):
            proxy._resolve_registered_endpoint()

    def test_foreign_account_ownership_is_rejected(
        self, proxy, gateway_alb, registry_parameter, monkeypatch
    ):
        monkeypatch.setenv("AWS_ACCOUNT_ID", "000000000000")
        with pytest.raises(RuntimeError, match="invalid ownership"):
            proxy._resolve_registered_endpoint()

    def test_alb_without_gateway_tag_is_rejected(
        self, proxy, gateway_alb, floci_account, monkeypatch
    ):
        # A second internal ALB with no ownership tags, registered under a
        # different project: the DNS shape and account both check out, but the
        # Gateway marker is absent — resolution must refuse it.
        elbv2 = gateway_alb["elbv2"]
        ec2 = boto3.client("ec2")
        vpc = ec2.create_vpc(CidrBlock="10.43.0.0/16")["Vpc"]["VpcId"]
        subnets = [
            ec2.create_subnet(VpcId=vpc, CidrBlock=cidr, AvailabilityZone=zone)["Subnet"][
                "SubnetId"
            ]
            for cidr, zone in (("10.43.1.0/24", "us-east-1a"), ("10.43.2.0/24", "us-east-1b"))
        ]
        rogue_project = f"gco{uuid.uuid4().hex[:8]}"
        rogue = elbv2.create_load_balancer(
            Name=f"{rogue_project}-gw", Subnets=subnets, Scheme="internal", Type="application"
        )["LoadBalancers"][0]
        ssm = boto3.client("ssm")
        ssm.put_parameter(
            Name=f"/{rogue_project}/alb-hostname-us-east-1",
            Value=rogue["DNSName"],
            Type="String",
            Overwrite=True,
        )

        monkeypatch.setenv("PROJECT_NAME", rogue_project)
        with pytest.raises(RuntimeError, match="not owned by the GCO cluster"):
            proxy._resolve_registered_endpoint()


# ---------------------------------------------------------------------------
# ga-registration: SSM registry half + tag-based ALB discovery half
# ---------------------------------------------------------------------------


class TestGaRegistrationDiscoveryHalves:
    @pytest.fixture()
    def handler(self, verified_floci_endpoint: str):
        return load_lambda_module("ga-registration")

    def test_ssm_registry_round_trip(self, handler, gateway_alb):
        project = f"gco{uuid.uuid4().hex[:8]}"
        handler.store_alb_hostname_in_ssm(
            "us-east-1", gateway_alb["dns_name"], "us-east-1", project
        )
        parameter = boto3.client("ssm").get_parameter(Name=f"/{project}/alb-hostname-us-east-1")[
            "Parameter"
        ]
        assert parameter["Value"] == gateway_alb["dns_name"]

        handler.delete_alb_hostname_from_ssm("us-east-1", "us-east-1", project)
        with pytest.raises(boto3.client("ssm").exceptions.ParameterNotFound):
            boto3.client("ssm").get_parameter(Name=f"/{project}/alb-hostname-us-east-1")

        # Idempotent cleanup: already-absent is tolerated, strict or not.
        handler.delete_alb_hostname_from_ssm("us-east-1", "us-east-1", project)
        handler.delete_alb_hostname_from_ssm("us-east-1", "us-east-1", project, strict=True)

    def test_tag_discovery_returns_only_the_exactly_owned_alb(self, handler, gateway_alb):
        dns_name, arn, _state = handler.find_platform_alb_by_tags(
            gateway_alb["elbv2"], gateway_alb["cluster_name"]
        )
        assert (dns_name, arn) == (gateway_alb["dns_name"], gateway_alb["arn"])

        missing = handler.find_platform_alb_by_tags(
            gateway_alb["elbv2"], "some-other-cluster-us-east-1"
        )
        assert missing == (None, None, None), (
            "an ALB whose cluster tag does not match exactly must never be adopted"
        )

    def test_hostname_discovery_requires_exact_ownership(self, handler, gateway_alb):
        dns_name, arn, _state = handler.find_alb_by_gateway_hostname(
            gateway_alb["elbv2"], gateway_alb["dns_name"], gateway_alb["cluster_name"]
        )
        assert (dns_name, arn) == (gateway_alb["dns_name"], gateway_alb["arn"])

        rejected = handler.find_alb_by_gateway_hostname(
            gateway_alb["elbv2"], gateway_alb["dns_name"], "impostor-cluster"
        )
        assert rejected == (None, None, None)
