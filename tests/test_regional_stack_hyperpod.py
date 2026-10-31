"""Regional-stack wiring for SageMaker HyperPod (``cdk.json`` ``hyperpod``).

Covers what ``GCORegionalStack`` synthesizes for the ``hyperpod`` block
through :class:`gco.stacks.hyperpod_cluster.HyperpodCluster`:

* HyperPod off (the default): no SageMaker cluster, no HYPERPOD_LINUX access
  entry, no ``{{HYPERPOD_*}}`` replacements (so the gated manifests stay out
  and are pruned), and no NVIDIA device plugin in the chart list;
* HyperPod on: the ``AWS::SageMaker::Cluster`` orchestrated by the regional
  EKS cluster, one instance group per configured group carrying the
  capacity-provider label, the HyperPod taint, deep health checks, the
  lifecycle script and the optional Training Plan; the execution role with
  the managed policy and exact grants; the HYPERPOD_LINUX access entry; the
  node security group and the reciprocal EKS rule; the lifecycle script put
  into the regional shared bucket; the ``vpc-cni``, ``kube-proxy`` and
  ``coredns`` EKS add-ons that make self-managed nodes work on the Auto Mode
  cluster (pinned builds, kube-proxy by cluster minor, the CNI's network
  policy agent following ``eks_cluster.network_policy_enforcement``, the
  DaemonSets ordered before the SageMaker cluster); the dependency on the
  convergence trigger; the replacements and the chart; the exact-key outputs;
* construct-level guards (zone selection, no subnets, an EKS minor without a
  kube-proxy pin, network policy off).

Stacks synthesize against ``MockConfigLoader`` with Docker image assets and
the Helm installer patched out, like ``tests/test_regional_stack_pcs.py``.
"""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import MagicMock, patch

import aws_cdk as cdk
import pytest
from aws_cdk import assertions
from aws_cdk import aws_ec2 as ec2
from aws_cdk import aws_iam as iam
from aws_cdk import aws_kms as kms
from aws_cdk import aws_s3 as s3

from gco import hyperpod_config as hp
from gco.stacks import constants
from gco.stacks import hyperpod_cluster as hc
from gco.stacks import regional_stack as rs
from tests.test_regional_stack import MockConfigLoader, TestRegionalStackSynthesis

#: ``AmazonEKS_CNI_Policy`` as published (the VPC CNI's needs): HyperPod's
#: documented EKS execution-role policy is this list plus ECR and Pod Identity.
_CNI_POLICY_ACTIONS = frozenset(
    {
        "ec2:AssignPrivateIpAddresses",
        "ec2:AttachNetworkInterface",
        "ec2:CreateNetworkInterface",
        "ec2:CreateTags",
        "ec2:DeleteNetworkInterface",
        "ec2:DescribeInstanceTypes",
        "ec2:DescribeInstances",
        "ec2:DescribeNetworkInterfaces",
        "ec2:DescribeSecurityGroups",
        "ec2:DescribeSubnets",
        "ec2:DescribeTags",
        "ec2:DetachNetworkInterface",
        "ec2:ModifyNetworkInterfaceAttribute",
        "ec2:UnassignPrivateIpAddresses",
    }
)

_ACCOUNT = "123456789012"
_REGION = "us-east-1"
_IMAGE_URI = f"{_ACCOUNT}.dkr.ecr.{_REGION}.amazonaws.com/test:latest"
_AZS = ["us-east-1a", "us-east-1b", "us-east-1c"]
_AZ_CONTEXT = {f"availability-zones:account={_ACCOUNT}:region={_REGION}": _AZS}
TRAINING_PLAN = f"arn:aws:sagemaker:{_REGION}:{_ACCOUNT}:training-plan/p5-week"

FULL: dict[str, Any] = {
    "enabled": True,
    "cluster_name": "research-hyperpod",
    "node_recovery": "None",
    "availability_zone": "us-east-1b",
    "instance_groups": [
        {
            "name": "p5",
            "instance_type": "ml.p5.48xlarge",
            "instance_count": 2,
            "threads_per_core": 2,
            "deep_health_checks": ["InstanceStress", "InstanceConnectivity"],
            "training_plan_arn": TRAINING_PLAN,
            "root_volume_size_gb": 500,
        },
        {
            "name": "cpu",
            "instance_type": "ml.c5.xlarge",
            "instance_count": 0,
            "deep_health_checks": [],
        },
    ],
    "queue": {"max_cpu": "2000", "max_memory": "8Ti", "max_gpu": "32"},
    "removal_policy": "retain",
}


class _HyperpodConfig(MockConfigLoader):
    """A loader whose ``hyperpod`` block is given."""

    def __init__(self, app: Any, *, hyperpod: dict[str, Any] | None) -> None:
        super().__init__(app)
        self._hyperpod = hyperpod

    def get_hyperpod_config(self, region: str | None = None) -> dict[str, Any]:
        return hp.validate_hyperpod_config(self._hyperpod)


def _synth(
    hyperpod: dict[str, Any] | None, *, name: str = "gco-test-us-east-1"
) -> tuple[rs.GCORegionalStack, dict[str, Any]]:
    app = cdk.App(context=dict(_AZ_CONTEXT))
    config = _HyperpodConfig(app, hyperpod=hyperpod)
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


def _of_type(resources: dict[str, Any], type_name: str) -> dict[str, dict[str, Any]]:
    return {k: v for k, v in resources.items() if v["Type"] == type_name}


def _replacements(resources: dict[str, Any]) -> dict[str, Any]:
    return resources["HelmInstallCharts"]["Properties"]["ImageReplacements"]


def _outputs(stack: rs.GCORegionalStack) -> dict[str, Any]:
    return assertions.Template.from_stack(stack).to_json()["Outputs"]


def _statements(resources: dict[str, Any], prefix: str) -> list[dict[str, Any]]:
    statements: list[dict[str, Any]] = []
    for logical_id, resource in resources.items():
        if resource["Type"] == "AWS::IAM::Policy" and logical_id.startswith(prefix):
            statements.extend(resource["Properties"]["PolicyDocument"]["Statement"])
    return statements


def _by_sid(statements: list[dict[str, Any]], sid: str) -> dict[str, Any]:
    [statement] = [s for s in statements if s.get("Sid") == sid]
    return statement


class TestHyperpodOff:
    def test_default_block_synthesizes_nothing_for_hyperpod(self) -> None:
        stack, resources = _synth(None)
        assert stack.hyperpod is None
        assert not _of_type(resources, "AWS::SageMaker::Cluster")
        entries = _of_type(resources, "AWS::EKS::AccessEntry")
        assert not [e for e in entries.values() if e["Properties"].get("Type") == "HYPERPOD_LINUX"]
        replacements = _replacements(resources)
        assert not [key for key in replacements if key.startswith("{{HYPERPOD_")]
        assert "nvidia-device-plugin" not in stack._get_enabled_helm_charts()
        assert not [key for key in _outputs(stack) if key.startswith("Hyperpod")]
        # A Region without HyperPod stays a pure Auto Mode cluster: none of the
        # mixed-compute add-ons (Auto Mode provides networking and DNS itself).
        addon_names = {
            r["Properties"]["AddonName"] for r in _of_type(resources, "AWS::EKS::Addon").values()
        }
        assert not addon_names & {"vpc-cni", "kube-proxy", "coredns"}

    def test_config_doubles_without_the_getter_read_as_off(self) -> None:
        app = cdk.App(context=dict(_AZ_CONTEXT))
        config = MockConfigLoader(app)
        assert not hasattr(config, "get_hyperpod_config")
        with (
            patch("gco.stacks.regional_stack.ecr_assets.DockerImageAsset") as mock_docker,
            patch.object(
                rs.GCORegionalStack,
                "_create_helm_installer_lambda",
                TestRegionalStackSynthesis._mock_helm_installer,
            ),
        ):
            mock_docker.return_value = MagicMock(image_uri=_IMAGE_URI)
            stack = rs.GCORegionalStack(
                app,
                "gco-test-us-east-1",
                config=config,
                region=_REGION,
                auth_secret_arn=f"arn:aws:secretsmanager:us-east-2:{_ACCOUNT}:secret:x",
                env=cdk.Environment(account=_ACCOUNT, region=_REGION),
            )
        assert stack.hyperpod is None
        assert stack._hyperpod_active() is False


@pytest.fixture(scope="module")
def synthesized() -> tuple[rs.GCORegionalStack, dict[str, Any]]:
    """One synth of the full block for every HyperPod-on assertion (~40 s)."""
    return _synth(FULL)


class TestHyperpodOn:
    def test_cluster_is_orchestrated_by_the_regional_eks_cluster(self, synthesized) -> None:
        stack, resources = synthesized
        assert stack.hyperpod is not None
        [(_logical_id, cluster)] = _of_type(resources, "AWS::SageMaker::Cluster").items()
        props = cluster["Properties"]
        assert props["ClusterName"] == "research-hyperpod"
        assert props["NodeRecovery"] == "None"
        assert "Fn::GetAtt" in json.dumps(props["Orchestrator"]["Eks"]["ClusterArn"])
        [sg_ref] = props["VpcConfig"]["SecurityGroupIds"]
        assert sg_ref["Fn::GetAtt"][0].startswith("HyperpodSecurityGroup")
        assert sg_ref["Fn::GetAtt"][1] == "GroupId"
        assert len(props["VpcConfig"]["Subnets"]) == 1
        assert sorted(tag["Key"] for tag in props["Tags"]) == [
            "Project",
            "gco:hyperpod-cluster",
            "gco:project",
        ]
        assert cluster["DeletionPolicy"] == "Retain"
        assert cluster["UpdateReplacePolicy"] == "Retain"
        # Created after the convergence trigger, the access entry, the SG and the script.
        depends = set(cluster["DependsOn"])
        assert "HelmInstallCharts" in depends
        assert any(dep.startswith("HyperpodNodeAccessEntry") for dep in depends)
        assert any(dep.startswith("HyperpodLifecycleScript") for dep in depends)

    def test_zone_pin_selects_that_subnet(self, synthesized) -> None:
        stack, _resources = synthesized
        assert stack.hyperpod is not None
        assert stack.hyperpod.subnet.availability_zone == "us-east-1b"

    def test_instance_groups_carry_labels_taint_checks_storage_and_plan(self, synthesized) -> None:
        _stack, resources = synthesized
        [cluster] = _of_type(resources, "AWS::SageMaker::Cluster").values()
        groups = {g["InstanceGroupName"]: g for g in cluster["Properties"]["InstanceGroups"]}
        assert list(groups) == ["p5", "cpu"]
        p5 = groups["p5"]
        assert p5["InstanceType"] == "ml.p5.48xlarge"
        assert p5["InstanceCount"] == 2
        assert p5["ThreadsPerCore"] == 2
        assert p5["OnStartDeepHealthChecks"] == ["InstanceStress", "InstanceConnectivity"]
        assert p5["TrainingPlanArn"] == TRAINING_PLAN
        assert p5["KubernetesConfig"]["Labels"] == {
            "gco.aws/capacity-provider": "hyperpod",
            "gco.aws/hyperpod-instance-group": "p5",
        }
        assert p5["KubernetesConfig"]["Taints"] == [
            {"Key": "gco.aws/hyperpod", "Value": "true", "Effect": "NoSchedule"}
        ]
        assert p5["InstanceStorageConfigs"] == [
            {"EbsVolumeConfig": {"RootVolume": True, "VolumeSizeInGB": 500}}
        ]
        assert p5["LifeCycleConfig"]["OnCreate"] == "on_create.sh"
        source = json.dumps(p5["LifeCycleConfig"]["SourceS3Uri"])
        assert "hyperpod/lifecycle/" in source and "RegionalSharedBucket" in source
        assert "Fn::GetAtt" in json.dumps(p5["ExecutionRole"])
        cpu = groups["cpu"]
        assert cpu["InstanceCount"] == 0
        assert "OnStartDeepHealthChecks" not in cpu
        assert "TrainingPlanArn" not in cpu
        assert cpu["InstanceStorageConfigs"][0]["EbsVolumeConfig"]["VolumeSizeInGB"] == 100

    def test_execution_role_has_the_managed_policy_and_exact_grants(self, synthesized) -> None:
        _stack, resources = synthesized
        [(_logical_id, role)] = [
            (k, v)
            for k, v in _of_type(resources, "AWS::IAM::Role").items()
            if k.startswith("HyperpodExecutionRole")
        ]
        trust = role["Properties"]["AssumeRolePolicyDocument"]["Statement"]
        assert trust[0]["Principal"] == {"Service": "sagemaker.amazonaws.com"}
        managed = json.dumps(role["Properties"]["ManagedPolicyArns"])
        assert "AmazonSageMakerClusterInstanceRolePolicy" in managed
        statements = _statements(resources, "HyperpodExecutionRole")
        node = _by_sid(statements, "EksNodeNetworkingAndImages")
        assert set(node["Action"]) == set(hc.EKS_NODE_ACTIONS)
        assert node["Resource"] == "*"
        # HyperPod's documented EKS execution-role policy is the VPC CNI's
        # policy plus image pulls and Pod Identity: aws-node runs on the nodes
        # with the node role, so every AmazonEKS_CNI_Policy action is present.
        assert _CNI_POLICY_ACTIONS.issubset({*hc.EKS_NODE_ACTIONS, *hc.ENI_TAG_ACTIONS})
        tags = _by_sid(statements, "EksNodeInterfaceTags")
        assert tags["Action"] == "ec2:CreateTags"
        assert json.dumps(tags["Resource"]).endswith(
            f':ec2:{_REGION}:{_ACCOUNT}:network-interface/*"]]}}'
        )
        read = _by_sid(statements, "HyperpodLifecycleScriptRead")
        assert read["Action"] == "s3:GetObject"
        assert "hyperpod/lifecycle/*" in json.dumps(read["Resource"])
        listing = _by_sid(statements, "HyperpodLifecycleScriptList")
        assert listing["Action"] == "s3:ListBucket"
        assert listing["Condition"] == {"StringLike": {"s3:prefix": ["hyperpod/lifecycle/*"]}}
        assert _by_sid(statements, "HyperpodLifecycleScriptDecrypt")["Action"] == "kms:Decrypt"
        assert role["DeletionPolicy"] == "Retain"

    def test_nodes_join_through_a_hyperpod_linux_access_entry(self, synthesized) -> None:
        _stack, resources = synthesized
        [entry] = [
            e
            for e in _of_type(resources, "AWS::EKS::AccessEntry").values()
            if e["Properties"].get("Type") == "HYPERPOD_LINUX"
        ]
        assert "AccessPolicies" not in entry["Properties"]
        assert "HyperpodExecutionRole" in json.dumps(entry["Properties"]["PrincipalArn"])
        assert "GCOEksCluster" in json.dumps(entry["Properties"]["ClusterName"])

    def test_security_group_admits_itself_and_the_eks_cluster_both_ways(self, synthesized) -> None:
        stack, resources = synthesized
        assert stack.hyperpod is not None
        [(sg_id, sg)] = [
            (k, v)
            for k, v in _of_type(resources, "AWS::EC2::SecurityGroup").items()
            if k.startswith("HyperpodSecurityGroup")
        ]
        assert sg["Properties"]["GroupName"] == "gco-test-hyperpod-sg-us-east-1"
        ingress = [
            r["Properties"]
            for r in _of_type(resources, "AWS::EC2::SecurityGroupIngress").values()
            if r["Properties"].get("IpProtocol") == "-1" and sg_id in json.dumps(r["Properties"])
        ]
        # Self, from the cluster SG into the HyperPod SG, and from the HyperPod SG into the cluster SG.
        assert len(ingress) == 3
        targets = {json.dumps(r["GroupId"]) for r in ingress}
        assert len(targets) == 2
        assert sg["DeletionPolicy"] == "Retain"

    def test_lifecycle_script_is_published_to_the_shared_bucket(self, synthesized) -> None:
        _stack, resources = synthesized
        [(_logical_id, resource)] = [
            (k, v)
            for k, v in resources.items()
            if v["Type"] == "Custom::AWS" and k.startswith("HyperpodLifecycleScript")
        ]
        create = (
            json.loads(resource["Properties"]["Create"])
            if isinstance(resource["Properties"]["Create"], str)
            else resource["Properties"]["Create"]
        )
        rendered = json.dumps(create)
        assert '"action":"putObject"' in rendered.replace(" ", "") or "putObject" in rendered
        assert "hyperpod/lifecycle/on_create.sh" in rendered
        assert hc.LIFECYCLE_SCRIPT.read_text(encoding="utf-8").splitlines()[0] == "#!/bin/bash"
        assert "gco hyperpod on_create" in rendered
        delete = json.dumps(resource["Properties"]["Delete"])
        assert "deleteObject" in delete
        statements = _statements(resources, "AwsCustomResourceRole")
        put = _by_sid(statements, "HyperpodLifecycleScript")
        assert set(put["Action"]) == {"s3:PutObject", "s3:DeleteObject"}
        assert set(_by_sid(statements, "HyperpodLifecycleScriptKms")["Action"]) == {
            "kms:GenerateDataKey",
            "kms:Decrypt",
        }

    def test_replacements_chart_and_outputs(self, synthesized) -> None:
        stack, resources = synthesized
        replacements = _replacements(resources)
        assert replacements["{{HYPERPOD_ENABLED}}"] == "true"
        assert replacements["{{HYPERPOD_HMA_IMAGE}}"] == hp.health_monitoring_agent_image(_REGION)
        assert replacements["{{HYPERPOD_QUEUE_MAX_GPU}}"] == "32"
        assert replacements["{{HYPERPOD_QUEUE_MAX_MEMORY}}"] == "8Ti"
        charts = stack._get_enabled_helm_charts()
        assert "nvidia-device-plugin" in charts
        assert charts.index("nvidia-device-plugin") > charts.index("kueue") or charts[-1] in (
            "kueue",
            "nvidia-device-plugin",
        )
        outputs = _outputs(stack)
        assert {
            "HyperpodClusterArn",
            "HyperpodClusterName",
            "HyperpodExecutionRoleArn",
            "HyperpodSecurityGroupId",
            "HyperpodInstanceGroups",
        } <= set(outputs)
        assert outputs["HyperpodClusterName"]["Value"] == "research-hyperpod"
        assert json.loads(outputs["HyperpodInstanceGroups"]["Value"]) == ["p5", "cpu"]

    def test_mixed_compute_addons_make_the_nodes_usable_on_auto_mode(self, synthesized) -> None:
        """The VPC CNI, kube-proxy and CoreDNS add-ons exist only with HyperPod on.

        Auto Mode serves networking and DNS only to its own nodes; the three
        add-ons are what let SageMaker's instances run pods. The DaemonSets
        gate the SageMaker cluster (a joining node turns Ready at once);
        CoreDNS runs on the Auto Mode nodes and is not ordered.
        """
        stack, resources = synthesized
        assert stack.hyperpod is not None
        addons = {
            r["Properties"]["AddonName"]: r
            for r in _of_type(resources, "AWS::EKS::Addon").values()
            if r["Properties"]["AddonName"] in {"vpc-cni", "kube-proxy", "coredns"}
        }
        assert set(addons) == {"vpc-cni", "kube-proxy", "coredns"}
        assert addons["vpc-cni"]["Properties"]["AddonVersion"] == constants.EKS_ADDON_VPC_CNI
        assert addons["coredns"]["Properties"]["AddonVersion"] == constants.EKS_ADDON_COREDNS
        # MockConfigLoader deploys 1.37; kube-proxy follows the cluster minor.
        kube_proxy = addons["kube-proxy"]["Properties"]["AddonVersion"]
        assert kube_proxy == constants.EKS_ADDON_KUBE_PROXY_BY_MINOR["1.37"]
        for name, addon in addons.items():
            props = addon["Properties"]
            assert props["ResolveConflicts"] == "OVERWRITE", name
            assert props["PreserveOnDelete"] is False, name
            assert "GCOEksCluster" in json.dumps(props["ClusterName"]), name
        assert json.loads(addons["vpc-cni"]["Properties"]["ConfigurationValues"]) == {
            "enableNetworkPolicy": "true"
        }
        assert "ConfigurationValues" not in addons["kube-proxy"]["Properties"]
        assert "ConfigurationValues" not in addons["coredns"]["Properties"]
        [cluster] = _of_type(resources, "AWS::SageMaker::Cluster").values()
        depends = set(cluster["DependsOn"])
        assert any(dep.startswith("HyperpodVpcCniAddon") for dep in depends)
        assert any(dep.startswith("HyperpodKubeProxyAddon") for dep in depends)
        assert not any(dep.startswith("HyperpodCoreDnsAddon") for dep in depends)
        assert set(stack.hyperpod.addons) == {"vpc-cni", "kube-proxy", "coredns"}
        # The applier converges after the CNI add-on exists: both write the
        # kube-system/amazon-vpc-cni switch, and the applier's rendering is final.
        converge = resources["HelmInstallCharts"]["DependsOn"]
        assert any(dep.startswith("HyperpodVpcCniAddon") for dep in converge)
        assert not any(dep.startswith("HyperpodKubeProxyAddon") for dep in converge)

    def test_default_block_names_the_cluster_after_the_project(self) -> None:
        stack, resources = _synth({"enabled": True})
        [cluster] = _of_type(resources, "AWS::SageMaker::Cluster").values()
        assert cluster["Properties"]["ClusterName"] == "gco-test-hyperpod"
        assert cluster["Properties"]["NodeRecovery"] == "Automatic"
        assert cluster["DeletionPolicy"] == "Delete"
        assert stack.hyperpod is not None
        assert stack.hyperpod.subnet.availability_zone == "us-east-1a"


class TestConstructGuards:
    @staticmethod
    def _bare_stack() -> tuple[cdk.Stack, dict[str, Any]]:
        app = cdk.App(context=dict(_AZ_CONTEXT))
        stack = cdk.Stack(app, "Bare", env=cdk.Environment(account=_ACCOUNT, region=_REGION))
        vpc = ec2.Vpc(stack, "Vpc", max_azs=2)
        key = kms.Key(stack, "Key")
        bucket = s3.Bucket(stack, "Bucket", encryption=s3.BucketEncryption.KMS, encryption_key=key)
        role = iam.Role(stack, "CrRole", assumed_by=iam.ServicePrincipal("lambda.amazonaws.com"))
        sg = ec2.SecurityGroup(stack, "EksSg", vpc=vpc)
        return stack, {
            "vpc": vpc,
            "subnets": vpc.private_subnets,
            "eks_cluster_name": "gco-test",
            "eks_cluster_arn": f"arn:aws:eks:{_REGION}:{_ACCOUNT}:cluster/gco-test",
            "eks_cluster_security_group": sg,
            "kubernetes_version": "1.36",
            "network_policy_enforcement": True,
            "shared_bucket": bucket,
            "shared_bucket_key": key,
            "custom_resource_role": role,
        }

    def test_unpinned_kube_proxy_minor_is_refused_at_synth(self) -> None:
        stack, kwargs = self._bare_stack()
        kwargs["kubernetes_version"] = "1.99"
        with pytest.raises(ValueError, match=r"no kube-proxy add-on build is pinned for EKS 1\.99"):
            hc.HyperpodCluster(
                stack,
                "Hyperpod",
                config=hp.validate_hyperpod_config({"enabled": True}),
                project_name="gco",
                region=_REGION,
                **kwargs,
            )

    def test_network_policy_off_turns_the_cni_agent_off(self) -> None:
        stack, kwargs = self._bare_stack()
        kwargs["network_policy_enforcement"] = False
        construct = hc.HyperpodCluster(
            stack,
            "Hyperpod",
            config=hp.validate_hyperpod_config({"enabled": True}),
            project_name="gco",
            region=_REGION,
            **kwargs,
        )
        resources = assertions.Template.from_stack(stack).to_json()["Resources"]
        [cni] = [
            r
            for r in _of_type(resources, "AWS::EKS::Addon").values()
            if r["Properties"]["AddonName"] == "vpc-cni"
        ]
        assert json.loads(cni["Properties"]["ConfigurationValues"]) == {
            "enableNetworkPolicy": "false"
        }
        assert construct.addons["kube-proxy"].addon_version == constants.kube_proxy_addon_version(
            "1.36"
        )

    def test_unknown_zone_names_the_available_ones(self) -> None:
        stack, kwargs = self._bare_stack()
        config = hp.validate_hyperpod_config({"enabled": True, "availability_zone": "us-east-1f"})
        with pytest.raises(
            ValueError, match=r"us-east-1f.*EKS-supported zones: us-east-1a, us-east-1b"
        ):
            hc.HyperpodCluster(
                stack, "Hyperpod", config=config, project_name="gco", region=_REGION, **kwargs
            )

    def test_no_subnets_is_refused(self) -> None:
        stack, kwargs = self._bare_stack()
        kwargs["subnets"] = []
        with pytest.raises(ValueError, match="at least one subnet"):
            hc.HyperpodCluster(
                stack,
                "Hyperpod",
                config=hp.validate_hyperpod_config({"enabled": True}),
                project_name="gco",
                region=_REGION,
                **kwargs,
            )
