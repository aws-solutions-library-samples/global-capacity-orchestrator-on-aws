"""Regional-stack wiring for AWS PCS (``cdk.json`` ``pcs``).

Covers what ``GCORegionalStack`` synthesizes for the ``pcs`` block through
:class:`gco.stacks.pcs_cluster.PcsCluster`:

* PCS off (the default): no PCS resources, no grants, and the always-emitted
  manifest tokens rendered empty so the API Deployment still applies;
* ``existing`` mode: the ``GetCluster`` facts custom resource through the
  shared custom-resource role, exact ``pcs:GetCluster`` and Secrets Manager
  grants on the manifest-processor role, the backend list and cluster id in
  the manifest replacements, the NetworkPolicy gate with the VPC CIDR or
  ``endpoint_cidr``, coexistence with Slinky, and the stack outputs;
* ``managed`` mode: the cluster, its security group, the node role under
  ``/aws-pcs/`` with exact grants, one launch template and compute node group
  per declared group (every purchase option, EFA on one and many network
  cards, a placement group, ODCR / Capacity Block / interruptible targeting,
  the three AMI sources with one lookup per kind and architecture), the
  queues with ``Default=YES`` on the default one, the login node, the EFS
  home access point, the storage security-group rules and the node lifecycle
  actions, in two synths (everything on, everything optional off) plus
  construct-level guards on a bare stack.

Stacks synthesize against ``MockConfigLoader`` with Docker image assets and
the Helm installer patched out, like ``tests/test_regional_stack.py``. The
CDK availability-zone context is cached in the app so the VPC's subnets carry
literal zones and node groups can be pinned to one.
"""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any
from unittest.mock import MagicMock, patch

import aws_cdk as cdk
import pytest
from aws_cdk import assertions

from gco import pcs_config as pc
from gco.stacks import regional_stack as rs
from tests.test_regional_stack import MockConfigLoader, TestRegionalStackSynthesis

_ACCOUNT = "123456789012"
_REGION = "us-east-1"
_IMAGE_URI = f"{_ACCOUNT}.dkr.ecr.{_REGION}.amazonaws.com/test:latest"
CLUSTER_ID = "pcs_0a1b2c3d4e"


class _PcsConfig(MockConfigLoader):
    """A loader whose ``pcs`` block is given, with the Slurm chart and FSx optionally on."""

    def __init__(
        self,
        app: Any,
        *,
        pcs: dict[str, Any] | None,
        slurm: bool = False,
        fsx_enabled: bool = False,
    ) -> None:
        super().__init__(app, fsx_enabled=fsx_enabled)
        self._pcs = pcs
        self._slurm = slurm

    def get_pcs_config(self, region: str | None = None) -> dict[str, Any]:
        return pc.validate_pcs_config(self._pcs, fsx_enabled=self._fsx_enabled)

    def get_slurm_config(self) -> dict[str, Any]:
        from gco.slurm_config import validate_slurm_config

        return validate_slurm_config({"enabled": self._slurm} if self._slurm else None)


def _synth(
    pcs: dict[str, Any] | None,
    *,
    name: str,
    slurm: bool = False,
    fsx_enabled: bool = False,
    context: dict[str, Any] | None = None,
) -> tuple[rs.GCORegionalStack, dict[str, Any]]:
    app = cdk.App(context=context or {})
    config = _PcsConfig(app, pcs=pcs, slurm=slurm, fsx_enabled=fsx_enabled)
    with (
        patch("gco.stacks.regional_stack.ecr_assets.DockerImageAsset") as mock_docker,
        patch.object(
            rs.GCORegionalStack,
            "_create_helm_installer_lambda",
            TestRegionalStackSynthesis._mock_helm_installer,
        ),
    ):
        mock_image = MagicMock()
        mock_image.image_uri = _IMAGE_URI
        mock_docker.return_value = mock_image
        stack = rs.GCORegionalStack(
            app,
            name,
            config=config,
            region=_REGION,
            auth_secret_arn=(
                f"arn:aws:secretsmanager:us-east-2:{_ACCOUNT}:secret:gco-test/api-gateway-auth-token"
            ),
            env=cdk.Environment(account=_ACCOUNT, region=_REGION),
        )
    return stack, assertions.Template.from_stack(stack).to_json()["Resources"]


#: The shipped cdk.json keeps the Slinky chart off; a context without a
#: ``helm`` block would read as on (the historical default for charts).
_SLINKY_OFF = {"helm": {"slurm": {"enabled": False}}}
#: CDK's cached availability-zone lookup: with it the VPC spans these three
#: zones and every subnet's ``availability_zone`` is a literal, which is what
#: lets a node group be pinned to one zone.
_AZS = ["us-east-1a", "us-east-1b", "us-east-1c"]
_AZ_CONTEXT = {f"availability-zones:account={_ACCOUNT}:region={_REGION}": _AZS}

AMI_ID = "ami-0123456789abcdef0"
ODCR_ID = "cr-0123456789abcdef0"
CAPACITY_BLOCK_ID = "cr-0fedcba9876543210"
INTERRUPTIBLE_ID = "cr-0aaaaaaaaaaaaaaaa"

#: Every managed feature in one block: six node groups covering every
#: purchase option, every AMI source and every launch-template shape, three
#: queues, a custom login node on Graviton, accounting that never purges,
#: cluster-level custom settings and a retained removal policy.
MANAGED_FULL: dict[str, Any] = {
    "enabled": True,
    "mode": "managed",
    "identity": {"name": "hpc", "uid": 2000, "gid": 3000, "gids": [4000], "dir": "/home/hpc"},
    "managed": {
        "cluster_name": "gco-test-hpc",
        "slurm_version": "26.05",
        "size": "MEDIUM",
        "accounting": {"enabled": True, "purge_days": -1},
        "scale_down_idle_seconds": 300,
        "storage": {"home": "efs", "scratch": "fsx"},
        "login": {
            "enabled": True,
            "instance_type": "t4g.small",
            "ami": "sample",
            "availability_zones": ["us-east-1a"],
        },
        "node_groups": [
            {"name": "gco-compute", "instance_types": ["c6i.large", "c6i.xlarge"]},
            {
                "name": "gpu",
                "instance_types": ["p5.48xlarge"],
                "ami": "dlami",
                "efa": True,
                "efa_interfaces": 4,
                "placement_group": True,
                "availability_zones": ["us-east-1a"],
                "max_nodes": 8,
                "slurm_custom_settings": {"Weight": 10},
                "gres_custom_settings": [{"Name": "gpu", "Type": "h100", "Count": 8}],
                "scale_down_idle_seconds": 120,
            },
            {
                "name": "hpc",
                "instance_types": ["hpc6a.48xlarge"],
                "efa": True,
                "availability_zones": ["us-east-1b"],
                "capacity_reservation": {"type": "odcr", "id": ODCR_ID},
            },
            {
                "name": "spot",
                "instance_types": ["c6i.large"],
                "purchase_option": "SPOT",
                "spot_allocation_strategy": "capacity-optimized",
                "max_nodes": 20,
            },
            {
                "name": "spot-default",
                "instance_types": ["m6i.large"],
                "purchase_option": "SPOT",
            },
            {
                "name": "block",
                "instance_types": ["p5.48xlarge"],
                "ami": "dlami",
                "availability_zones": ["us-east-1c"],
                "capacity_reservation": {"type": "capacity_block", "id": CAPACITY_BLOCK_ID},
                "min_nodes": 2,
                "max_nodes": 2,
            },
            {
                "name": "shared",
                "instance_types": ["c7g.large"],
                "ami": AMI_ID,
                "availability_zones": ["us-east-1a"],
                "capacity_reservation": {"type": "interruptible", "id": INTERRUPTIBLE_ID},
            },
        ],
        "queues": [
            {
                "name": "gco",
                "node_groups": ["gco-compute", "spot", "spot-default"],
                "default": True,
            },
            {
                "name": "gpu",
                "node_groups": ["gpu", "block"],
                "slurm_custom_settings": {"MaxTime": "3-00:00:00"},
            },
            {"name": "hpc", "node_groups": ["hpc", "shared"]},
        ],
        "lifecycle_logs": {"enabled": True},
        # Metrics on, with an operator CommunicationParameters the stack must
        # merge enable_http into rather than replace.
        "metrics": {"enabled": True},
        "slurm_custom_settings": {
            "MaxJobCount": 20000,
            "CommunicationParameters": "block_null_hash",
        },
        "slurmdbd_custom_settings": {"PurgeJobAfter": "90days"},
        "removal_policy": "retain",
    },
}

#: The other extreme: no shared storage, no accounting, no login node, no log
#: forwarding, so no lifecycle action, access point, asset or EFS grant.
MANAGED_MINIMAL: dict[str, Any] = {
    "enabled": True,
    "mode": "managed",
    "identity": {"dir": "/local/ec2-user"},
    "managed": {
        "accounting": {"enabled": False},
        "storage": {"home": "none", "scratch": "none"},
        "login": {"enabled": False},
        "lifecycle_logs": {"enabled": False},
    },
}


@pytest.fixture(scope="module")
def pcs_off():
    return _synth(None, name="test-pcs-off", context=_SLINKY_OFF)


@pytest.fixture(scope="module")
def pcs_existing():
    return _synth(
        {"enabled": True, "mode": "existing", "cluster_id": CLUSTER_ID},
        name="test-pcs-existing",
        context=_SLINKY_OFF,
    )


@pytest.fixture(scope="module")
def pcs_managed_full():
    return _synth(
        MANAGED_FULL,
        name="test-pcs-managed-full",
        fsx_enabled=True,
        context={**_SLINKY_OFF, **_AZ_CONTEXT},
    )


@pytest.fixture(scope="module")
def pcs_managed_minimal():
    return _synth(MANAGED_MINIMAL, name="test-pcs-managed-minimal", context=_SLINKY_OFF)


@pytest.fixture(scope="module")
def pcs_existing_with_slinky_and_peered_cidr():
    return _synth(
        {
            "enabled": True,
            "mode": "existing",
            "cluster_id": CLUSTER_ID,
            "endpoint_cidr": "192.168.0.0/20",
            "identity": {"name": "hpc", "uid": 2000, "gid": 2000},
        },
        name="test-pcs-existing-slinky",
        slurm=True,
        context={"helm": {"slurm": {"enabled": True}}},
    )


def _replacements(resources: dict[str, Any]) -> dict[str, Any]:
    return resources["HelmInstallCharts"]["Properties"]["ImageReplacements"]


def _statements(resources: dict[str, Any], role_prefix: str) -> list[dict[str, Any]]:
    statements: list[dict[str, Any]] = []
    for logical_id, resource in resources.items():
        if resource["Type"] != "AWS::IAM::Policy" or not logical_id.startswith(role_prefix):
            continue
        statements.extend(resource["Properties"]["PolicyDocument"]["Statement"])
    return statements


def _by_sid(statements: list[dict[str, Any]], sid: str) -> dict[str, Any]:
    [statement] = [s for s in statements if s.get("Sid") == sid]
    return statement


def _custom_resources(resources: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {
        logical_id: resource
        for logical_id, resource in resources.items()
        if resource["Type"] == "Custom::AWS" and "Pcs" in logical_id
    }


def _cluster_arn(cluster_id: str) -> dict[str, Any]:
    """The rendered ARN: the partition is a deploy-time pseudo parameter."""
    return {
        "Fn::Join": [
            "",
            ["arn:", {"Ref": "AWS::Partition"}, f":pcs:{_REGION}:{_ACCOUNT}:cluster/{cluster_id}"],
        ]
    }


def _vpc_logical_id(resources: dict[str, Any]) -> str:
    [logical_id] = [k for k, v in resources.items() if v["Type"] == "AWS::EC2::VPC"]
    return logical_id


class TestPcsOff:
    def test_no_pcs_resources_or_grants(self, pcs_off) -> None:
        stack, resources = pcs_off
        assert stack.pcs is None
        assert not _custom_resources(resources)
        assert not any(r["Type"].startswith("AWS::PCS::") for r in resources.values())
        for statement in _statements(resources, "ManifestProcessorRole"):
            assert "pcs:GetCluster" not in statement.get("Action", [])
            assert statement.get("Sid") != "PcsReadJwtSigningKey"

    def test_the_always_emitted_tokens_render_empty(self, pcs_off) -> None:
        _stack, resources = pcs_off
        replacements = _replacements(resources)
        assert replacements["{{SLURM_BACKENDS}}"] == ""
        assert replacements["{{PCS_CLUSTER_ID}}"] == ""
        assert replacements["{{PCS_IDENTITY_JSON}}"] == ""
        assert replacements["{{PCS_DEFAULT_PARTITION}}"] == ""
        assert "{{PCS_ENABLED}}" not in replacements
        assert "{{PCS_RESTAPI_CIDR}}" not in replacements

    def test_a_loader_without_the_getter_reads_as_off(self) -> None:
        stack, resources = _synth(None, name="test-pcs-no-getter")
        # MockConfigLoader subclasses in other test modules lack get_pcs_config.
        stack.config = MockConfigLoader()
        assert stack._pcs_config()["enabled"] is False
        stack.config = MagicMock()
        assert stack._pcs_config()["enabled"] is False
        # Without a helm block the harness reads the Slinky chart as on, so
        # the backend list names it even though PCS is off.
        assert _replacements(resources)["{{SLURM_BACKENDS}}"] == "slinky"


class TestExistingMode:
    def test_the_facts_custom_resource_reads_the_cluster(self, pcs_existing) -> None:
        stack, resources = pcs_existing
        assert stack.pcs is not None
        assert stack.pcs.mode == "existing"
        assert stack.pcs.cluster_id == CLUSTER_ID
        assert stack.pcs.cluster_arn.endswith(f":pcs:{_REGION}:{_ACCOUNT}:cluster/{CLUSTER_ID}")
        [(logical_id, facts)] = _custom_resources(resources).items()
        assert "Facts" in logical_id
        create = json.loads(facts["Properties"]["Create"])
        update = json.loads(facts["Properties"]["Update"])
        for call in (create, update):
            assert call["service"] == "PCS"
            assert call["action"] == "getCluster"
            assert call["parameters"] == {"clusterIdentifier": CLUSTER_ID}
            assert "cluster.slurmConfiguration.jwtAuth.jwtKey.secretArn" in call["outputPaths"]
        assert create["physicalResourceId"] == {"id": f"gco-test-pcs-facts-{_REGION}"}
        assert facts["Properties"]["InstallLatestAwsSdk"] is True
        # The shared custom-resource role runs it, and holds GetCluster.
        statement = _by_sid(_statements(resources, "AwsCustomResourceRole"), "PcsGetCluster")
        assert statement["Action"] == "pcs:GetCluster"
        assert statement["Resource"] == _cluster_arn(CLUSTER_ID)

    def test_the_api_role_gets_exactly_two_reads(self, pcs_existing) -> None:
        _stack, resources = pcs_existing
        statements = _statements(resources, "ManifestProcessorRole")
        get_cluster = _by_sid(statements, "PcsGetCluster")
        assert get_cluster["Action"] == "pcs:GetCluster"
        assert get_cluster["Resource"] == _cluster_arn(CLUSTER_ID)
        secret = _by_sid(statements, "PcsReadJwtSigningKey")
        assert sorted(secret["Action"]) == [
            "secretsmanager:DescribeSecret",
            "secretsmanager:GetSecretValue",
        ]
        # The secret ARN is the one GetCluster reports, not a wildcard.
        [(facts_id, _facts)] = _custom_resources(resources).items()
        assert secret["Resource"] == {
            "Fn::GetAtt": [facts_id, "cluster.slurmConfiguration.jwtAuth.jwtKey.secretArn"]
        }
        pcs_actions = {
            action
            for statement in statements
            for action in (
                statement["Action"]
                if isinstance(statement["Action"], list)
                else [statement["Action"]]
            )
            if action.startswith("pcs:")
        }
        assert pcs_actions == {"pcs:GetCluster"}

    def test_manifest_replacements_name_pcs_alone(self, pcs_existing) -> None:
        _stack, resources = pcs_existing
        replacements = _replacements(resources)
        assert replacements["{{SLURM_BACKENDS}}"] == "pcs"
        assert replacements["{{PCS_ENABLED}}"] == "true"
        assert replacements["{{PCS_CLUSTER_ID}}"] == CLUSTER_ID
        assert replacements["{{PCS_DEFAULT_PARTITION}}"] == ""
        assert json.loads(replacements["{{PCS_IDENTITY_JSON}}"])["name"] == "ec2-user"
        # The VPC CIDR is only known at deploy time.
        assert replacements["{{PCS_RESTAPI_CIDR}}"] == {
            "Fn::GetAtt": [_vpc_logical_id(resources), "CidrBlock"]
        }
        # Slinky's own always-emitted token still says "off".
        assert replacements["{{SLURM_API_ENABLED}}"] == "false"

    def test_no_pcs_infrastructure_is_created(self, pcs_existing) -> None:
        _stack, resources = pcs_existing
        assert not any(r["Type"].startswith("AWS::PCS::") for r in resources.values())
        assert not any(
            r["Type"] == "AWS::EC2::LaunchTemplate" and "Pcs" in logical_id
            for logical_id, r in resources.items()
        )

    def test_outputs_have_stable_keys(self, pcs_existing) -> None:
        stack, resources = pcs_existing
        outputs = assertions.Template.from_stack(stack).to_json()["Outputs"]
        # The construct is nested, yet the keys carry no hash: the CLI reads
        # them by name from describe-stacks.
        pcs_outputs = {k: v["Value"] for k, v in outputs.items() if k.startswith("Pcs")}
        [(facts_id, _facts)] = _custom_resources(resources).items()
        assert pcs_outputs == {
            "PcsClusterId": CLUSTER_ID,
            "PcsMode": "existing",
            "PcsRestApiPort": "6820",
            "PcsSlurmVersion": {"Fn::GetAtt": [facts_id, "cluster.scheduler.version"]},
            "PcsRestApiMode": {
                "Fn::GetAtt": [facts_id, "cluster.slurmConfiguration.slurmRest.mode"]
            },
        }

    def test_the_converge_trigger_waits_for_the_facts(self, pcs_existing) -> None:
        _stack, resources = pcs_existing
        [(facts_id, _facts)] = _custom_resources(resources).items()
        depends_on = resources["HelmInstallCharts"].get("DependsOn", [])
        assert facts_id in depends_on


def _of_type(resources: dict[str, Any], type_name: str) -> dict[str, dict[str, Any]]:
    return {k: v for k, v in resources.items() if v["Type"] == type_name}


def _named(resources: dict[str, Any], type_name: str) -> dict[str, dict[str, Any]]:
    """PCS node groups / queues keyed by their ``Name`` property."""
    return {v["Properties"]["Name"]: v for v in _of_type(resources, type_name).values()}


def _logical_id(resources: dict[str, Any], type_name: str, name: str) -> str:
    [logical_id] = [
        k for k, v in _of_type(resources, type_name).items() if v["Properties"]["Name"] == name
    ]
    return logical_id


def _launch_template(resources: dict[str, Any], node_group: str) -> dict[str, Any]:
    """The launch template whose ``gco:pcs-node-group`` tag names ``node_group``."""
    [template] = [
        v
        for v in _of_type(resources, "AWS::EC2::LaunchTemplate").values()
        if {"Key": "gco:pcs-node-group", "Value": node_group}
        in v["Properties"]["TagSpecifications"][0]["Tags"]
    ]
    return template


def _private_subnets_by_zone(resources: dict[str, Any]) -> dict[str, str]:
    """``availability zone -> logical id`` of the VPC's private subnets."""
    return {
        v["Properties"]["AvailabilityZone"]: k
        for k, v in _of_type(resources, "AWS::EC2::Subnet").items()
        if v["Properties"].get("MapPublicIpOnLaunch") is False
    }


def _sids(statements: list[dict[str, Any]]) -> set[str]:
    return {s["Sid"] for s in statements if "Sid" in s}


def _ingress_rules(resources: dict[str, Any], *, to_group: str) -> list[dict[str, Any]]:
    return [
        v["Properties"]
        for v in _of_type(resources, "AWS::EC2::SecurityGroupIngress").values()
        if v["Properties"]["GroupId"] == {"Fn::GetAtt": [to_group, "GroupId"]}
    ]


#: The characters EC2 accepts in a security group rule description (the API's
#: own error text); anything else fails the stack at CREATE time, after the
#: rest of the template is already in flight.
_EC2_RULE_DESCRIPTION = re.compile(r"[a-zA-Z0-9. _\-:/()#,@\[\]+=&;{}!$*]{0,255}")


def _rule_descriptions(resources: dict[str, Any]) -> dict[str, str]:
    """Every security group rule description in the template, standalone and inline."""
    found: dict[str, str] = {}
    for logical_id, resource in resources.items():
        properties = resource.get("Properties", {})
        if resource["Type"] in {"AWS::EC2::SecurityGroupIngress", "AWS::EC2::SecurityGroupEgress"}:
            if "Description" in properties:
                found[logical_id] = properties["Description"]
        elif resource["Type"] == "AWS::EC2::SecurityGroup":
            for direction in ("SecurityGroupIngress", "SecurityGroupEgress"):
                for index, rule in enumerate(properties.get(direction, [])):
                    if "Description" in rule:
                        found[f"{logical_id}.{direction}[{index}]"] = rule["Description"]
    return found


class TestManagedModeCluster:
    def test_the_cluster_is_created_from_the_block(self, pcs_managed_full) -> None:
        stack, resources = pcs_managed_full
        assert stack.pcs is not None and stack.pcs.mode == "managed"
        assert stack.pcs.cluster_name == "gco-test-hpc"
        [(cluster_id, cluster)] = _of_type(resources, "AWS::PCS::Cluster").items()
        assert cluster["DeletionPolicy"] == "Retain"
        assert cluster["UpdateReplacePolicy"] == "Retain"
        props = cluster["Properties"]
        assert props["Name"] == "gco-test-hpc"
        assert props["Size"] == "MEDIUM"
        assert props["Scheduler"] == {"Type": "SLURM", "Version": "26.05"}
        # One subnet (PCS takes exactly one) and the cluster's own SG.
        subnets = _private_subnets_by_zone(resources)
        assert props["Networking"]["SubnetIds"] == [{"Ref": subnets["us-east-1a"]}]
        [sg_id] = _of_type(
            {k: v for k, v in resources.items() if k.startswith("PcsSecurityGroup")},
            "AWS::EC2::SecurityGroup",
        )
        assert props["Networking"]["SecurityGroupIds"] == [{"Fn::GetAtt": [sg_id, "GroupId"]}]
        slurm = props["SlurmConfiguration"]
        assert slurm["SlurmRest"] == {"Mode": "STANDARD"}
        assert slurm["Accounting"] == {"Mode": "STANDARD", "DefaultPurgeTimeInDays": -1}
        assert slurm["ScaleDownIdleTimeInSeconds"] == 300
        # The operator's settings plus the two PCS documents for its metrics,
        # enable_http merged into the operator's CommunicationParameters.
        assert slurm["SlurmCustomSettings"] == [
            {"ParameterName": "MaxJobCount", "ParameterValue": "20000"},
            {
                "ParameterName": "CommunicationParameters",
                "ParameterValue": "block_null_hash,enable_http",
            },
            {"ParameterName": "MetricsType", "ParameterValue": "metrics/openmetrics"},
        ]
        assert slurm["SlurmdbdCustomSettings"] == [
            {"ParameterName": "PurgeJobAfter", "ParameterValue": "90days"}
        ]
        assert "CgroupCustomSettings" not in slurm
        assert props["Tags"] == {
            "Project": "gco-test",
            "gco:project": "gco-test",
            "gco:pcs-cluster": "gco-test-hpc",
        }
        # The cluster id and ARN the rest of the stack wires are the resource's.
        assert stack.pcs.cluster_id == stack.pcs.cluster_resource.attr_id
        assert _replacements(resources)["{{PCS_CLUSTER_ID}}"] == {"Fn::GetAtt": [cluster_id, "Id"]}
        assert _replacements(resources)["{{PCS_DEFAULT_PARTITION}}"] == "gco"
        assert json.loads(_replacements(resources)["{{PCS_IDENTITY_JSON}}"])["name"] == "hpc"

    def test_the_security_group_is_the_documented_permissive_shape(self, pcs_managed_full) -> None:
        _stack, resources = pcs_managed_full
        [(sg_id, sg)] = [
            (k, v)
            for k, v in _of_type(resources, "AWS::EC2::SecurityGroup").items()
            if k.startswith("PcsSecurityGroup")
        ]
        assert sg["Properties"]["GroupName"] == "gco-test-pcs-sg-us-east-1"
        assert sg["Properties"]["SecurityGroupEgress"][0]["CidrIp"] == "0.0.0.0/0"
        rules = _ingress_rules(resources, to_group=sg_id)
        self_rule = [
            r for r in rules if r["SourceSecurityGroupId"] == {"Fn::GetAtt": [sg_id, "GroupId"]}
        ]
        assert [r["IpProtocol"] for r in self_rule] == ["-1"]
        [rest] = [r for r in rules if r.get("FromPort") == 6820]
        assert rest["ToPort"] == 6820
        assert rest["SourceSecurityGroupId"]["Fn::GetAtt"][1] == "ClusterSecurityGroupId"
        # Metrics on: slurmctld's port from the same source, for Prometheus.
        [metrics] = [r for r in rules if r.get("FromPort") == 6817]
        assert metrics["ToPort"] == 6817 and metrics["IpProtocol"] == "tcp"
        assert metrics["SourceSecurityGroupId"]["Fn::GetAtt"][1] == "ClusterSecurityGroupId"
        assert len(rules) == 3

    def test_every_rule_description_is_one_ec2_accepts(self, pcs_managed_full) -> None:
        """EC2 validates rule descriptions at CREATE time, not at synth.

        The first live deploy of the PCS block rolled back the whole regional
        stack on ``Invalid rule description`` for the self rule's ``<->``;
        every description the template carries (the PCS group's and the EFS
        and FSx rules it adds, plus the rest of the stack's) must stay inside
        the set the API accepts, so the failure is a unit test, not an hour
        into a deploy.
        """
        _stack, resources = pcs_managed_full
        descriptions = _rule_descriptions(resources)
        pcs_rules = {k: v for k, v in descriptions.items() if "PcsSecurityGroup" in k}
        assert len(pcs_rules) >= 3, sorted(descriptions)
        assert "Slurm controller to/from compute nodes, srun, and EFA between nodes" in set(
            pcs_rules.values()
        )
        rejected = {
            k: v
            for k, v in descriptions.items()
            if not isinstance(v, str) or _EC2_RULE_DESCRIPTION.fullmatch(v) is None
        }
        assert rejected == {}

    def test_metrics_resolve_the_controller_address_for_the_scrape(self, pcs_managed_full) -> None:
        """``managed.metrics.enabled`` wires the ScrapeConfig's static target.

        The address comes from the provider's second resource type, which
        reads the created cluster (so it waits for ACTIVE) with a
        ``pcs:GetCluster`` grant written against that cluster's ARN; the
        kubectl-applier gets the gate and the address as deploy-time tokens.
        """
        stack, resources = pcs_managed_full
        assert stack.pcs.metrics_enabled is True
        [(cluster_id, _cluster)] = _of_type(resources, "AWS::PCS::Cluster").items()
        [(lookup_id, lookup)] = _of_type(resources, "Custom::PcsControllerLookup").items()
        assert lookup["Properties"]["ClusterId"] == {"Fn::GetAtt": [cluster_id, "Id"]}
        assert lookup["Properties"]["Region"] == _REGION
        # Same provider as the AMI lookups.
        [ami_lookup] = list(_of_type(resources, "Custom::PcsAmiLookup").values())[:1]
        assert lookup["Properties"]["ServiceToken"] == ami_lookup["Properties"]["ServiceToken"]
        statements = _statements(resources, "PcsAmiLookupFunctionServiceRole")
        get_cluster = _by_sid(statements, "DescribePcsCluster")
        assert get_cluster["Action"] == "pcs:GetCluster"
        assert get_cluster["Resource"] == {"Fn::GetAtt": [cluster_id, "Arn"]}
        replacements = _replacements(resources)
        assert replacements["{{PCS_METRICS_ENABLED}}"] == "true"
        assert replacements["{{PCS_CONTROLLER_ADDRESS}}"] == {"Fn::GetAtt": [lookup_id, "Address"]}
        assert cdk.Token.is_unresolved(stack.pcs.controller_address)
        # One lookup per provider: asking again returns the same resource's token.
        again = stack.pcs.ami_lookup.controller_address(
            cluster_id=stack.pcs.cluster_id, cluster_arn=stack.pcs.cluster_arn
        )
        assert stack.resolve(again) == stack.resolve(stack.pcs.controller_address)
        assert len(_of_type(resources, "Custom::PcsControllerLookup")) == 1

    def test_the_facts_and_api_grants_follow_the_created_cluster(self, pcs_managed_full) -> None:
        _stack, resources = pcs_managed_full
        [(cluster_id, _cluster)] = _of_type(resources, "AWS::PCS::Cluster").items()
        [(facts_id, facts)] = _custom_resources(resources).items()
        assert cluster_id in facts["DependsOn"]
        # The parameters embed the cluster id token, so Create is a Join.
        assert "getCluster" in json.dumps(facts["Properties"]["Create"])
        statements = _statements(resources, "ManifestProcessorRole")
        get_cluster = _by_sid(statements, "PcsGetCluster")
        assert get_cluster["Resource"] == {"Fn::GetAtt": [cluster_id, "Arn"]}
        secret = _by_sid(statements, "PcsReadJwtSigningKey")
        assert secret["Resource"]["Fn::GetAtt"][0] == facts_id

    def test_outputs(self, pcs_managed_full) -> None:
        stack, resources = pcs_managed_full
        outputs = {
            k: v["Value"]
            for k, v in assertions.Template.from_stack(stack).to_json()["Outputs"].items()
            if k.startswith("Pcs")
        }
        [cluster_id] = _of_type(resources, "AWS::PCS::Cluster")
        [sg_id] = [
            k
            for k in _of_type(resources, "AWS::EC2::SecurityGroup")
            if k.startswith("PcsSecurityGroup")
        ]
        [role_id] = [
            k for k in _of_type(resources, "AWS::IAM::Role") if k.startswith("PcsNodeRole")
        ]
        assert outputs["PcsClusterName"] == "gco-test-hpc"
        assert outputs["PcsMode"] == "managed"
        assert outputs["PcsClusterId"] == {"Fn::GetAtt": [cluster_id, "Id"]}
        assert outputs["PcsSecurityGroupId"] == {"Fn::GetAtt": [sg_id, "GroupId"]}
        assert outputs["PcsNodeRoleArn"] == {"Fn::GetAtt": [role_id, "Arn"]}
        assert outputs["PcsLoginNodeGroupId"] == {
            "Fn::GetAtt": [_logical_id(resources, "AWS::PCS::ComputeNodeGroup", "login"), "Id"]
        }
        assert set(outputs) == {
            "PcsClusterName",
            "PcsMode",
            "PcsClusterId",
            "PcsSecurityGroupId",
            "PcsNodeRoleArn",
            "PcsLoginNodeGroupId",
            "PcsRestApiPort",
            "PcsSlurmVersion",
            "PcsRestApiMode",
        }


class TestManagedModeNodeGroups:
    def test_every_declared_group_and_the_login_node_exist(self, pcs_managed_full) -> None:
        stack, resources = pcs_managed_full
        groups = _named(resources, "AWS::PCS::ComputeNodeGroup")
        assert set(groups) == {
            "gco-compute",
            "gpu",
            "hpc",
            "spot",
            "spot-default",
            "block",
            "shared",
            "login",
        }
        assert set(stack.pcs.node_groups) == set(groups) - {"login"}
        assert stack.pcs.login_node_group is not None
        [(cluster_id, _)] = _of_type(resources, "AWS::PCS::Cluster").items()
        [(profile_id, profile)] = _of_type(resources, "AWS::IAM::InstanceProfile").items()
        assert profile["Properties"]["Path"] == "/aws-pcs/"
        for name, group in groups.items():
            props = group["Properties"]
            assert props["ClusterId"] == {"Fn::GetAtt": [cluster_id, "Id"]}
            assert props["IamInstanceProfileArn"] == {"Fn::GetAtt": [profile_id, "Arn"]}
            assert group["DeletionPolicy"] == "Retain"
            # The launch template is pinned to its latest version so a
            # template change rolls the nodes.
            template_ref = props["CustomLaunchTemplate"]["TemplateId"]["Ref"]
            assert props["CustomLaunchTemplate"]["Version"] == {
                "Fn::GetAtt": [template_ref, "LatestVersionNumber"]
            }
            assert props["Tags"]["gco:pcs-node-group"] == name
            assert props["NodeLifecycleActions"]["ScriptCachingPolicy"] == "CACHE_ONCE"

    def test_scaling_purchase_options_and_instance_types(self, pcs_managed_full) -> None:
        _stack, resources = pcs_managed_full
        groups = _named(resources, "AWS::PCS::ComputeNodeGroup")

        def scaling(name: str) -> tuple[int, int]:
            s = groups[name]["Properties"]["ScalingConfiguration"]
            return s["MinInstanceCount"], s["MaxInstanceCount"]

        def types(name: str) -> list[str]:
            return [c["InstanceType"] for c in groups[name]["Properties"]["InstanceConfigs"]]

        assert types("gco-compute") == ["c6i.large", "c6i.xlarge"]
        assert scaling("gco-compute") == (0, 4)
        assert groups["gco-compute"]["Properties"]["PurchaseOption"] == "ONDEMAND"
        assert "SpotOptions" not in groups["gco-compute"]["Properties"]
        assert "SlurmConfiguration" not in groups["gco-compute"]["Properties"]

        assert groups["spot"]["Properties"]["PurchaseOption"] == "SPOT"
        assert groups["spot"]["Properties"]["SpotOptions"] == {
            "AllocationStrategy": "capacity-optimized"
        }
        assert scaling("spot") == (0, 20)
        # PCS's own default allocation strategy when none is configured.
        assert groups["spot-default"]["Properties"]["PurchaseOption"] == "SPOT"
        assert "SpotOptions" not in groups["spot-default"]["Properties"]

        # Derived from the reservation type.
        assert groups["hpc"]["Properties"]["PurchaseOption"] == "ONDEMAND"
        assert groups["block"]["Properties"]["PurchaseOption"] == "CAPACITY_BLOCK"
        assert scaling("block") == (2, 2)
        assert groups["shared"]["Properties"]["PurchaseOption"] == (
            "INTERRUPTIBLE_CAPACITY_RESERVATION"
        )

        assert types("login") == ["t4g.small"]
        assert scaling("login") == (1, 1)
        assert groups["login"]["Properties"]["PurchaseOption"] == "ONDEMAND"
        # Node-group level Slurm settings, gres records and idle override.
        assert groups["gpu"]["Properties"]["SlurmConfiguration"] == {
            "SlurmCustomSettings": [{"ParameterName": "Weight", "ParameterValue": "10"}],
            "GresCustomSettings": [{"Name": "gpu", "Type": "h100", "Count": "8"}],
            "ScaleDownIdleTimeInSeconds": 120,
        }

    def test_amis_are_looked_up_once_per_kind_and_architecture_or_passed_through(
        self, pcs_managed_full
    ) -> None:
        stack, resources = pcs_managed_full
        groups = _named(resources, "AWS::PCS::ComputeNodeGroup")
        lookups = _of_type(resources, "Custom::PcsAmiLookup")
        by_key = {
            (v["Properties"]["Kind"], v["Properties"]["Architecture"]): k
            for k, v in lookups.items()
        }
        assert set(by_key) == {("sample", "x86_64"), ("dlami", "x86_64"), ("sample", "arm64")}
        assert set(stack.pcs.ami_lookup.lookups) == set(by_key)
        for resource in lookups.values():
            assert resource["Properties"]["SlurmVersion"] == "26.05"
            assert resource["Properties"]["Region"] == _REGION
        # The label is the first group that asked for the pair.
        assert lookups[by_key[("sample", "x86_64")]]["Properties"]["NodeGroup"] == "gco-compute"
        assert lookups[by_key[("sample", "arm64")]]["Properties"]["NodeGroup"] == "login"

        def ami(name: str) -> Any:
            return groups[name]["Properties"]["AmiId"]

        sample_x86 = {"Fn::GetAtt": [by_key[("sample", "x86_64")], "ImageId"]}
        assert ami("gco-compute") == sample_x86
        assert ami("spot") == sample_x86
        assert ami("spot-default") == sample_x86
        assert ami("hpc") == sample_x86
        assert ami("gpu") == {"Fn::GetAtt": [by_key[("dlami", "x86_64")], "ImageId"]}
        assert ami("block") == ami("gpu")
        assert ami("login") == {"Fn::GetAtt": [by_key[("sample", "arm64")], "ImageId"]}
        assert ami("shared") == AMI_ID
        # One provider and one handler back every lookup.
        functions = {
            k: v
            for k, v in _of_type(resources, "AWS::Lambda::Function").items()
            if k.startswith("PcsAmiLookup")
        }
        [handler] = [v for k, v in functions.items() if "framework" not in k]
        assert handler["Properties"]["Handler"] == "handler.lambda_handler"
        assert len(functions) == 2

    def test_subnets_follow_the_availability_zones(self, pcs_managed_full) -> None:
        _stack, resources = pcs_managed_full
        groups = _named(resources, "AWS::PCS::ComputeNodeGroup")
        subnets = _private_subnets_by_zone(resources)
        assert set(subnets) == set(_AZS)

        def subnet_ids(name: str) -> list[str]:
            return [ref["Ref"] for ref in groups[name]["Properties"]["SubnetIds"]]

        assert subnet_ids("gpu") == [subnets["us-east-1a"]]
        assert subnet_ids("hpc") == [subnets["us-east-1b"]]
        assert subnet_ids("block") == [subnets["us-east-1c"]]
        assert subnet_ids("login") == [subnets["us-east-1a"]]
        # Named zones never go through the deploy-time lookup.
        assert not any(
            isinstance(groups[name]["Properties"]["SubnetIds"], dict)
            for name in ("gpu", "hpc", "block", "shared", "login")
        )

    def test_groups_without_zones_narrow_their_subnets_at_deploy_time(
        self, pcs_managed_full
    ) -> None:
        """``Custom::PcsSubnetLookup`` keeps the zones that offer every instance type.

        The second live deploy of the PCS block failed its node groups on
        ``The following instance type isn't offered in 1 or more of the
        cluster's Availability Zones``: the regional VPC spans every zone in
        us-east-1 and us-east-1e offers neither c6i.large nor t3.medium. A
        group without ``availability_zones`` now hands every private subnet
        and its instance types to the lookup and takes the subnets it returns.
        """
        stack, resources = pcs_managed_full
        groups = _named(resources, "AWS::PCS::ComputeNodeGroup")
        subnets = _private_subnets_by_zone(resources)
        lookups = _of_type(resources, "Custom::PcsSubnetLookup")
        by_group = {v["Properties"]["NodeGroup"]: k for k, v in lookups.items()}
        assert set(by_group) == {"gco-compute", "spot", "spot-default"}
        assert set(stack.pcs.ami_lookup.subnet_lookups) == set(by_group)
        for name, logical_id in by_group.items():
            properties = lookups[logical_id]["Properties"]
            # Every private subnet is a candidate; the handler drops the zones
            # that lack one of the (sorted, deduplicated) instance types.
            assert {ref["Ref"] for ref in properties["SubnetIds"]} == set(subnets.values())
            assert properties["Region"] == _REGION
            assert (
                properties["ServiceToken"]
                == _of_type(resources, "Custom::PcsAmiLookup").popitem()[1]["Properties"][
                    "ServiceToken"
                ]
            )
            [log_group] = [
                k
                for k in _of_type(resources, "AWS::Logs::LogGroup")
                if k.startswith("PcsAmiLookupProviderLogGroup")
            ]
            assert lookups[logical_id]["DependsOn"] == [log_group]
            assert groups[name]["Properties"]["SubnetIds"] == {
                "Fn::GetAtt": [logical_id, "SubnetIds"]
            }
        assert lookups[by_group["gco-compute"]]["Properties"]["InstanceTypes"] == [
            "c6i.large",
            "c6i.xlarge",
        ]
        assert lookups[by_group["spot"]]["Properties"]["InstanceTypes"] == ["c6i.large"]
        # The handler's role can describe subnets and offerings next to images.
        statements = _statements(resources, "PcsAmiLookupFunctionServiceRole")
        catalog = _by_sid(statements, "DescribeEc2Catalog")
        assert catalog["Action"] == [
            "ec2:DescribeImages",
            "ec2:DescribeInstanceTypeOfferings",
            "ec2:DescribeSubnets",
        ]
        assert catalog["Resource"] == "*"
        with pytest.raises(ValueError, match="already has a subnet lookup"):
            stack.pcs.ami_lookup.subnet_ids(
                node_group="spot", candidates=["subnet-1"], instance_types=["c6i.large"]
            )

    def test_node_groups_wait_for_the_role_and_the_storage(self, pcs_managed_full) -> None:
        _stack, resources = pcs_managed_full
        group = _named(resources, "AWS::PCS::ComputeNodeGroup")["gco-compute"]
        depends_on = set(group["DependsOn"])
        [role_id] = [
            k for k in _of_type(resources, "AWS::IAM::Role") if k.startswith("PcsNodeRole")
        ]
        [policy_id] = [
            k for k in _of_type(resources, "AWS::IAM::Policy") if k.startswith("PcsNodeRole")
        ]
        assert {role_id, policy_id} <= depends_on
        assert "GCOFsxLustre" in depends_on
        assert any(k.startswith("GCOEfsEfsMountTarget") for k in depends_on)
        assert any(
            k.startswith("EfsSecurityGroupfrom") and "PcsSecurityGroup" in k for k in depends_on
        )
        assert any(
            k.startswith("FsxSecurityGroupfrom") and "PcsSecurityGroup" in k for k in depends_on
        )


class TestManagedModeLaunchTemplates:
    def test_plain_groups_use_the_security_group_and_imdsv2(self, pcs_managed_full) -> None:
        _stack, resources = pcs_managed_full
        data = _launch_template(resources, "gco-compute")["Properties"]["LaunchTemplateData"]
        assert data["MetadataOptions"] == {
            "HttpEndpoint": "enabled",
            "HttpTokens": "required",
            "HttpPutResponseHopLimit": 2,
        }
        assert len(data["SecurityGroupIds"]) == 1
        for absent in (
            "NetworkInterfaces",
            "Placement",
            "CapacityReservationSpecification",
            "InstanceMarketOptions",
            "InstanceType",
            "ImageId",
            "UserData",
        ):
            assert absent not in data
        # Instances and volumes carry the cluster, group and Name tags.
        tagged = {
            t["ResourceType"]: {x["Key"]: x["Value"] for x in t["Tags"]}
            for t in data["TagSpecifications"]
        }
        assert set(tagged) == {"instance", "volume"}
        assert tagged["instance"]["Name"] == "gco-test-hpc-gco-compute"
        assert tagged["instance"]["gco:pcs-node-group"] == "gco-compute"
        # The ownership tag the project inventory recognizes on resources
        # CloudFormation did not create (PCS launches these instances).
        assert tagged["instance"]["gco:project"] == "gco-test"
        assert tagged["volume"]["gco:project"] == "gco-test"
        assert _launch_template(resources, "gco-compute")["DeletionPolicy"] == "Retain"

    def test_multi_card_efa_with_a_placement_group(self, pcs_managed_full) -> None:
        stack, resources = pcs_managed_full
        data = _launch_template(resources, "gpu")["Properties"]["LaunchTemplateData"]
        assert "SecurityGroupIds" not in data
        interfaces = data["NetworkInterfaces"]
        assert [
            (i["NetworkCardIndex"], i["DeviceIndex"], i["InterfaceType"]) for i in interfaces
        ] == [
            (0, 0, "interface"),
            (0, 1, "efa-only"),
            (1, 0, "efa-only"),
            (2, 0, "efa-only"),
            (3, 0, "efa-only"),
        ]
        groups = {json.dumps(i["Groups"]) for i in interfaces}
        assert len(groups) == 1 and "PcsSecurityGroup" in groups.pop()
        assert all(i["DeleteOnTermination"] is True for i in interfaces)
        [(pg_id, pg)] = _of_type(resources, "AWS::EC2::PlacementGroup").items()
        assert pg["Properties"]["Strategy"] == "cluster"
        assert data["Placement"] == {"GroupName": {"Ref": pg_id}}
        assert set(stack.pcs.placement_groups) == {"gpu"}

    def test_single_card_efa_with_a_targeted_odcr(self, pcs_managed_full) -> None:
        _stack, resources = pcs_managed_full
        data = _launch_template(resources, "hpc")["Properties"]["LaunchTemplateData"]
        [interface] = data["NetworkInterfaces"]
        assert (interface["NetworkCardIndex"], interface["DeviceIndex"]) == (0, 0)
        assert interface["InterfaceType"] == "efa"
        assert data["CapacityReservationSpecification"] == {
            "CapacityReservationTarget": {"CapacityReservationId": ODCR_ID}
        }
        # A targeted ODCR is consumed by plain On-Demand launches.
        assert "InstanceMarketOptions" not in data
        assert "Placement" not in data

    def test_capacity_block(self, pcs_managed_full) -> None:
        _stack, resources = pcs_managed_full
        data = _launch_template(resources, "block")["Properties"]["LaunchTemplateData"]
        assert data["InstanceMarketOptions"] == {"MarketType": "capacity-block"}
        assert data["CapacityReservationSpecification"]["CapacityReservationTarget"] == {
            "CapacityReservationId": CAPACITY_BLOCK_ID
        }
        # Capacity Blocks need the reserved type in the template itself.
        assert data["InstanceType"] == "p5.48xlarge"

    def test_interruptible_reservation(self, pcs_managed_full) -> None:
        _stack, resources = pcs_managed_full
        data = _launch_template(resources, "shared")["Properties"]["LaunchTemplateData"]
        assert data["InstanceMarketOptions"] == {"MarketType": "interruptible-capacity-reservation"}
        assert data["CapacityReservationSpecification"]["CapacityReservationTarget"] == {
            "CapacityReservationId": INTERRUPTIBLE_ID
        }
        assert "InstanceType" not in data

    def test_the_login_template_is_plain(self, pcs_managed_full) -> None:
        _stack, resources = pcs_managed_full
        data = _launch_template(resources, "login")["Properties"]["LaunchTemplateData"]
        assert "NetworkInterfaces" not in data and "Placement" not in data
        assert len(_of_type(resources, "AWS::EC2::LaunchTemplate")) == 8


class TestManagedModeQueues:
    def test_queues_map_node_groups_and_mark_the_default(self, pcs_managed_full) -> None:
        stack, resources = pcs_managed_full
        queues = _named(resources, "AWS::PCS::Queue")
        assert set(queues) == {"gco", "gpu", "hpc"} == set(stack.pcs.queues)
        [(cluster_id, _)] = _of_type(resources, "AWS::PCS::Cluster").items()

        def group_refs(name: str) -> list[str]:
            return [
                c["ComputeNodeGroupId"]["Fn::GetAtt"][0]
                for c in queues[name]["Properties"]["ComputeNodeGroupConfigurations"]
            ]

        def group_id(name: str) -> str:
            return _logical_id(resources, "AWS::PCS::ComputeNodeGroup", name)

        assert group_refs("gco") == [
            group_id("gco-compute"),
            group_id("spot"),
            group_id("spot-default"),
        ]
        assert group_refs("gpu") == [group_id("gpu"), group_id("block")]
        assert group_refs("hpc") == [group_id("hpc"), group_id("shared")]
        for queue in queues.values():
            assert queue["Properties"]["ClusterId"] == {"Fn::GetAtt": [cluster_id, "Id"]}
            assert queue["DeletionPolicy"] == "Retain"
        assert queues["gco"]["Properties"]["SlurmConfiguration"] == {
            "SlurmCustomSettings": [{"ParameterName": "Default", "ParameterValue": "YES"}]
        }
        assert queues["gpu"]["Properties"]["SlurmConfiguration"] == {
            "SlurmCustomSettings": [{"ParameterName": "MaxTime", "ParameterValue": "3-00:00:00"}]
        }
        assert "SlurmConfiguration" not in queues["hpc"]["Properties"]
        # The login node group is in no queue.
        login = _logical_id(resources, "AWS::PCS::ComputeNodeGroup", "login")
        assert login not in {ref for name in queues for ref in group_refs(name)}


class TestManagedModeStorageAndLifecycle:
    def test_the_home_access_point_is_rooted_at_the_pcs_path(self, pcs_managed_full) -> None:
        stack, resources = pcs_managed_full
        access_points = {
            k: v
            for k, v in _of_type(resources, "AWS::EFS::AccessPoint").items()
            if v["Properties"]["RootDirectory"]["Path"] == pc.PCS_HOME_EFS_PATH
        }
        [ap] = access_points.values()
        assert ap["Properties"]["RootDirectory"]["CreationInfo"] == {
            "OwnerUid": "2000",
            "OwnerGid": "3000",
            "Permissions": "755",
        }
        assert "PosixUser" not in ap["Properties"]
        assert ap["DeletionPolicy"] == "Retain"
        assert stack.pcs.home_access_point is not None
        # The jobs access point is untouched.
        assert len(_of_type(resources, "AWS::EFS::AccessPoint")) == 2

    def test_storage_security_groups_admit_the_nodes(self, pcs_managed_full) -> None:
        _stack, resources = pcs_managed_full
        [pcs_sg] = [
            k
            for k in _of_type(resources, "AWS::EC2::SecurityGroup")
            if k.startswith("PcsSecurityGroup")
        ]
        [efs_sg] = [
            k
            for k in _of_type(resources, "AWS::EC2::SecurityGroup")
            if k.startswith("EfsSecurityGroup")
        ]
        [fsx_sg] = [
            k
            for k in _of_type(resources, "AWS::EC2::SecurityGroup")
            if k.startswith("FsxSecurityGroup")
        ]
        source = {"Fn::GetAtt": [pcs_sg, "GroupId"]}
        efs_rules = [
            r
            for r in _ingress_rules(resources, to_group=efs_sg)
            if r["SourceSecurityGroupId"] == source
        ]
        assert [(r["FromPort"], r["ToPort"]) for r in efs_rules] == [(2049, 2049)]
        fsx_rules = [
            r
            for r in _ingress_rules(resources, to_group=fsx_sg)
            if r["SourceSecurityGroupId"] == source
        ]
        assert sorted((r["FromPort"], r["ToPort"]) for r in fsx_rules) == [(988, 988), (1018, 1023)]

    def test_the_lifecycle_actions_in_order(self, pcs_managed_full) -> None:
        _stack, resources = pcs_managed_full
        groups = _named(resources, "AWS::PCS::ComputeNodeGroup")
        actions = groups["gco-compute"]["Properties"]["NodeLifecycleActions"]
        scripts = actions["Stages"]["NodeBootstrapped"]
        assert [s["Name"] for s in scripts] == [
            "configure-cloudwatch-logs",
            "mount-efs-home",
            "mount-fsx-scratch",
            "ensure-identity-home",
        ]
        prefix = f"s3://aws-pcs-repo-{_REGION}/aws-pcs-node-lifecycle-scripts/"
        logs, home, scratch, identity = scripts
        assert logs["ScriptSource"] == {
            "ScriptLocation": prefix + "configure-cloudwatch-logs-v1.0.0.sh"
        }
        assert (logs["ExecutionPolicy"], logs["OnError"]) == ("FIRST_BOOT_ONLY", "CONTINUE")
        assert "Arguments" not in logs

        assert home["ScriptSource"] == {"ScriptLocation": prefix + "mount-efs-v1.0.0.sh"}
        assert (home["ExecutionPolicy"], home["OnError"]) == ("EVERY_BOOT", "TERMINATE")
        [efs_id] = [k for k, v in _of_type(resources, "AWS::EFS::FileSystem").items()]
        [ap_id] = [
            k
            for k, v in _of_type(resources, "AWS::EFS::AccessPoint").items()
            if v["Properties"]["RootDirectory"]["Path"] == pc.PCS_HOME_EFS_PATH
        ]
        assert home["Arguments"] == [
            "--file-system-id",
            {"Ref": efs_id},
            "--mount-point",
            "/home",
            "--options",
            {"Fn::Join": ["", ["tls,iam,accesspoint=", {"Ref": ap_id}]]},
        ]

        assert scratch["ScriptSource"] == {"ScriptLocation": prefix + "mount-fsx-lustre-v1.0.0.sh"}
        assert scratch["Arguments"] == [
            "--fsx-dns-name",
            {"Fn::GetAtt": ["GCOFsxLustre", "DNSName"]},
            "--mount-name",
            {"Fn::GetAtt": ["GCOFsxLustre", "LustreMountName"]},
            "--mount-point",
            "/scratch",
        ]
        assert (scratch["ExecutionPolicy"], scratch["OnError"]) == ("EVERY_BOOT", "TERMINATE")

        # GCO's own script ships as a stack asset, pinned by checksum.
        from gco.stacks.pcs_cluster import IDENTITY_HOME_SCRIPT

        assert identity["ScriptSource"]["ScriptLocation"].startswith("s3://cdk-")
        assert identity["ScriptSource"]["ScriptLocation"].endswith(".sh")
        assert (
            identity["ScriptSource"]["Checksum"]
            == hashlib.sha256(IDENTITY_HOME_SCRIPT.read_bytes()).hexdigest()
        )
        assert identity["Arguments"] == [
            "--home-base",
            "/home",
            "--name",
            "hpc",
            "--uid",
            "2000",
            "--gid",
            "3000",
        ]
        assert (identity["ExecutionPolicy"], identity["OnError"]) == ("EVERY_BOOT", "TERMINATE")
        # Every group, the login node included, runs the same actions.
        for group in groups.values():
            assert group["Properties"]["NodeLifecycleActions"] == actions


class TestManagedModeNodeRole:
    def test_the_role_path_and_managed_policies(self, pcs_managed_full) -> None:
        stack, resources = pcs_managed_full
        [(role_id, role)] = [
            (k, v)
            for k, v in _of_type(resources, "AWS::IAM::Role").items()
            if k.startswith("PcsNodeRole")
        ]
        assert role["Properties"]["Path"] == "/aws-pcs/"
        assert role["Properties"]["AssumeRolePolicyDocument"]["Statement"][0]["Principal"] == {
            "Service": "ec2.amazonaws.com"
        }
        managed = [json.dumps(arn) for arn in role["Properties"]["ManagedPolicyArns"]]
        assert any("policy/AWSPCSComputeNodePolicy" in arn for arn in managed)
        assert any("policy/AmazonSSMManagedInstanceCore" in arn for arn in managed)
        assert len(managed) == 2
        assert role["DeletionPolicy"] == "Retain"
        [(_profile_id, profile)] = _of_type(resources, "AWS::IAM::InstanceProfile").items()
        assert profile["Properties"]["Roles"] == [{"Ref": role_id}]
        assert stack.pcs.node_role is not None and stack.pcs.instance_profile is not None

    def test_the_inline_grants_are_exact(self, pcs_managed_full) -> None:
        _stack, resources = pcs_managed_full
        statements = _statements(resources, "PcsNodeRole")
        assert _sids(statements) == {
            "ReadPcsLifecycleScripts",
            "ReadIdentityHomeScript",
            "MountPcsHome",
            "ForwardPcsLifecycleLogs",
        }
        scripts = _by_sid(statements, "ReadPcsLifecycleScripts")
        assert scripts["Action"] == "s3:GetObject"
        assert json.dumps(scripts["Resource"]).endswith(
            f':s3:::aws-pcs-repo-{_REGION}/aws-pcs-node-lifecycle-scripts/*"]]}}'
        )
        asset = _by_sid(statements, "ReadIdentityHomeScript")
        assert asset["Action"] == "s3:GetObject"
        assert ":s3:::cdk-" in json.dumps(asset["Resource"]) and ".sh" in json.dumps(
            asset["Resource"]
        )
        mount = _by_sid(statements, "MountPcsHome")
        assert mount["Action"] == [
            "elasticfilesystem:ClientMount",
            "elasticfilesystem:ClientWrite",
            "elasticfilesystem:ClientRootAccess",
        ]
        [efs_id] = _of_type(resources, "AWS::EFS::FileSystem")
        assert mount["Resource"] == {"Fn::GetAtt": [efs_id, "Arn"]}
        assert "elasticfilesystem:AccessPointArn" in mount["Condition"]["StringEquals"]
        logs = _by_sid(statements, "ForwardPcsLifecycleLogs")
        assert logs["Action"] == [
            "logs:CreateLogGroup",
            "logs:CreateLogStream",
            "logs:PutLogEvents",
            "logs:DescribeLogStreams",
        ]
        [(cluster_id, _)] = _of_type(resources, "AWS::PCS::Cluster").items()
        rendered = json.dumps(logs["Resource"])
        assert (
            f':log-group:/aws/pcs/", {{"Fn::GetAtt": ["{cluster_id}", "Id"]}}, "/lifecycle"'
            in rendered
        )
        assert "/lifecycle:*" in rendered
        # No PCS action beyond registering reaches the node role (that one
        # comes from the managed policy).
        assert not any("pcs:" in json.dumps(s.get("Action")) for s in statements)


class TestManagedModeMinimal:
    def test_nothing_optional_is_created(self, pcs_managed_minimal) -> None:
        stack, resources = pcs_managed_minimal
        assert stack.pcs is not None and stack.pcs.mode == "managed"
        assert stack.pcs.cluster_name == "gco-test-pcs"
        [cluster] = _of_type(resources, "AWS::PCS::Cluster").values()
        assert cluster["DeletionPolicy"] == "Delete"
        assert cluster["Properties"]["Name"] == "gco-test-pcs"
        assert cluster["Properties"]["Size"] == "SMALL"
        assert cluster["Properties"]["Scheduler"]["Version"] == "25.11"
        slurm = cluster["Properties"]["SlurmConfiguration"]
        assert slurm["Accounting"] == {"Mode": "NONE"}
        assert slurm["SlurmRest"] == {"Mode": "STANDARD"}
        assert "SlurmCustomSettings" not in slurm and "SlurmdbdCustomSettings" not in slurm
        groups = _named(resources, "AWS::PCS::ComputeNodeGroup")
        assert set(groups) == {"gco-compute"}
        assert stack.pcs.login_node_group is None
        group = groups["gco-compute"]["Properties"]
        assert "NodeLifecycleActions" not in group
        assert group["PurchaseOption"] == "ONDEMAND"
        assert groups["gco-compute"]["DeletionPolicy"] == "Delete"
        assert len(_of_type(resources, "AWS::EC2::LaunchTemplate")) == 1
        assert len(_of_type(resources, "Custom::PcsAmiLookup")) == 1
        # The one group names no zones, so its subnets come from the lookup.
        [subnet_lookup] = _of_type(resources, "Custom::PcsSubnetLookup").values()
        assert subnet_lookup["Properties"]["NodeGroup"] == "gco-compute"
        assert subnet_lookup["Properties"]["InstanceTypes"] == ["c6i.large"]
        assert not _of_type(resources, "AWS::EC2::PlacementGroup")
        # Metrics off (the default): no controller lookup, no 6817 rule, no
        # observability gate, and the lookup role reads no cluster.
        assert stack.pcs.metrics_enabled is False and stack.pcs.controller_address == ""
        assert not _of_type(resources, "Custom::PcsControllerLookup")
        assert "{{PCS_METRICS_ENABLED}}" not in _replacements(resources)
        assert "{{PCS_CONTROLLER_ADDRESS}}" not in _replacements(resources)
        assert "DescribePcsCluster" not in _sids(
            _statements(resources, "PcsAmiLookupFunctionServiceRole")
        )
        # No PCS access point: only the jobs one the stack always creates.
        assert [
            v["Properties"]["RootDirectory"]["Path"]
            for v in _of_type(resources, "AWS::EFS::AccessPoint").values()
        ] == ["/gco-jobs"]
        assert stack.pcs.home_access_point is None and stack.pcs.identity_home_asset is None
        assert stack.pcs.lifecycle_actions is None
        statements = _statements(resources, "PcsNodeRole")
        assert _sids(statements) == {"ReadPcsLifecycleScripts"}
        [pcs_sg] = [
            k
            for k in _of_type(resources, "AWS::EC2::SecurityGroup")
            if k.startswith("PcsSecurityGroup")
        ]
        [efs_sg] = [
            k
            for k in _of_type(resources, "AWS::EC2::SecurityGroup")
            if k.startswith("EfsSecurityGroup")
        ]
        assert not [
            r
            for r in _ingress_rules(resources, to_group=efs_sg)
            if r["SourceSecurityGroupId"] == {"Fn::GetAtt": [pcs_sg, "GroupId"]}
        ]
        queues = _named(resources, "AWS::PCS::Queue")
        assert set(queues) == {"gco"}
        outputs = assertions.Template.from_stack(stack).to_json()["Outputs"]
        assert "PcsLoginNodeGroupId" not in outputs
        assert outputs["PcsClusterName"]["Value"] == "gco-test-pcs"
        assert _replacements(resources)["{{PCS_DEFAULT_PARTITION}}"] == "gco"
        assert (
            json.loads(_replacements(resources)["{{PCS_IDENTITY_JSON}}"])["dir"]
            == "/local/ec2-user"
        )


# ---------------------------------------------------------------------------
# Construct-level guards, on a bare stack (seconds, not a regional synth)
# ---------------------------------------------------------------------------


def _bare_stack(
    *,
    concrete: bool = True,
    zones: bool = True,
    private_subnets: bool = True,
) -> tuple[cdk.Stack, dict[str, Any]]:
    """A stack with just what ``PcsCluster`` needs around it."""
    from aws_cdk import aws_ec2 as ec2
    from aws_cdk import aws_efs as efs
    from aws_cdk import aws_iam as iam

    app = cdk.App(context=_AZ_CONTEXT if zones else {})
    stack = cdk.Stack(
        app,
        "bare",
        env=cdk.Environment(account=_ACCOUNT, region=_REGION) if concrete else None,
    )
    subnet_type = ec2.SubnetType.PRIVATE_WITH_EGRESS if private_subnets else ec2.SubnetType.PUBLIC
    vpc = ec2.Vpc(
        stack,
        "Vpc",
        max_azs=3,
        nat_gateways=1 if private_subnets else 0,
        subnet_configuration=[
            ec2.SubnetConfiguration(name="Public", subnet_type=ec2.SubnetType.PUBLIC, cidr_mask=24),
            *(
                [ec2.SubnetConfiguration(name="Private", subnet_type=subnet_type, cidr_mask=24)]
                if private_subnets
                else []
            ),
        ],
    )
    efs_sg = ec2.SecurityGroup(stack, "EfsSg", vpc=vpc)
    kwargs: dict[str, Any] = {
        "project_name": "gco-test",
        "vpc": vpc,
        "eks_cluster_security_group": ec2.SecurityGroup(stack, "EksSg", vpc=vpc),
        "api_role": iam.Role(stack, "Api", assumed_by=iam.ServicePrincipal("ec2.amazonaws.com")),
        "custom_resource_role": iam.Role(
            stack, "Cr", assumed_by=iam.ServicePrincipal("lambda.amazonaws.com")
        ),
        "efs_file_system": efs.FileSystem(stack, "Efs", vpc=vpc, security_group=efs_sg),
        "efs_security_group": efs_sg,
    }
    return stack, kwargs


class TestConstructGuards:
    def test_an_efs_home_needs_the_regional_efs(self) -> None:
        from gco.stacks.pcs_cluster import PcsCluster

        stack, kwargs = _bare_stack()
        kwargs["efs_file_system"] = None
        with pytest.raises(
            ValueError, match=r"storage\.home is 'efs' but the regional stack has no EFS"
        ):
            PcsCluster(stack, "Pcs", config=pc.validate_pcs_config(_managed()), **kwargs)

    def test_an_fsx_scratch_needs_the_regional_fsx(self) -> None:
        from gco.stacks.pcs_cluster import PcsCluster

        stack, kwargs = _bare_stack()
        # The loader said the region has FSx; the stack disagrees.
        config = pc.validate_pcs_config(_managed(storage={"scratch": "fsx"}), fsx_enabled=True)
        with pytest.raises(
            ValueError, match=r"scratch resolves to 'fsx' but the regional stack has no FSx"
        ):
            PcsCluster(stack, "Pcs", config=config, **kwargs)

    def test_the_controller_needs_a_private_subnet(self) -> None:
        from gco.stacks.pcs_cluster import PcsCluster

        stack, kwargs = _bare_stack(private_subnets=False)
        with pytest.raises(ValueError, match=r"no private subnets for the PCS controller"):
            PcsCluster(stack, "Pcs", config=pc.validate_pcs_config(_managed()), **kwargs)

    def test_availability_zones_need_an_environment_specific_synth(self) -> None:
        from gco.stacks.pcs_cluster import PcsCluster

        stack, kwargs = _bare_stack(concrete=False, zones=False)
        config = pc.validate_pcs_config(_managed(login={"availability_zones": ["us-east-1a"]}))
        with pytest.raises(
            ValueError,
            match=r"pcs\.managed\.login\.availability_zones needs an environment-specific synth",
        ):
            PcsCluster(stack, "Pcs", config=config, **kwargs)

    def test_availability_zones_must_have_a_private_subnet(self) -> None:
        from gco.stacks.pcs_cluster import PcsCluster

        stack, kwargs = _bare_stack()
        config = pc.validate_pcs_config(
            _managed(
                node_groups=[
                    {
                        "name": "far",
                        "instance_types": ["c6i.large"],
                        "availability_zones": ["us-east-1d"],
                    }
                ],
                queues=[{"name": "gco", "node_groups": ["far"], "default": True}],
            )
        )
        with pytest.raises(
            ValueError,
            match=r"node_groups\.far\.availability_zones names zone\(s\) without a private subnet .*us-east-1d.*spans us-east-1a, us-east-1b, us-east-1c",
        ):
            PcsCluster(stack, "Pcs", config=config, **kwargs)

    def test_a_bare_managed_cluster_synthesizes(self) -> None:
        """The construct stands on its own (the regional stack is one caller)."""
        from gco.stacks.pcs_cluster import PcsCluster

        stack, kwargs = _bare_stack()
        pcs_cluster = PcsCluster(stack, "Pcs", config=pc.validate_pcs_config(_managed()), **kwargs)
        template = assertions.Template.from_stack(stack).to_json()
        template_resources = template["Resources"]
        assert len(_of_type(template_resources, "AWS::PCS::Cluster")) == 1
        assert set(_named(template_resources, "AWS::PCS::ComputeNodeGroup")) == {
            "gco-compute",
            "login",
        }
        assert pcs_cluster.scratch == "none"
        assert pcs_cluster.home_on_efs is True


def _managed(**overrides: Any) -> dict[str, Any]:
    return {"enabled": True, "mode": "managed", "managed": overrides}


class TestExistingModeWithSlinky:
    def test_both_backends_and_the_peered_cidr(
        self, pcs_existing_with_slinky_and_peered_cidr
    ) -> None:
        _stack, resources = pcs_existing_with_slinky_and_peered_cidr
        replacements = _replacements(resources)
        assert replacements["{{SLURM_BACKENDS}}"] == "slinky,pcs"
        assert replacements["{{SLURM_API_ENABLED}}"] == "true"
        assert replacements["{{SLURM_ENABLED}}"] == "true"
        assert replacements["{{PCS_ENABLED}}"] == "true"
        assert replacements["{{PCS_RESTAPI_CIDR}}"] == "192.168.0.0/20"
        identity = json.loads(replacements["{{PCS_IDENTITY_JSON}}"])
        assert (identity["name"], identity["uid"], identity["gid"]) == ("hpc", 2000, 2000)
        assert identity["dir"] == "/home/hpc"
