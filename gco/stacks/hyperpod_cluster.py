"""Amazon SageMaker HyperPod as EKS-orchestrated capacity for a regional stack.

:class:`HyperpodCluster` synthesizes everything one Region's ``hyperpod`` block
asks for around the regional EKS cluster:

- the execution role the instance groups run as (the AWS-managed
  ``AmazonSageMakerClusterInstanceRolePolicy`` plus the ENI, ECR and Pod
  Identity grants HyperPod documents for EKS orchestration);
- the ``HYPERPOD_LINUX`` EKS access entry that lets those nodes join the
  cluster — the only authentication path, since the cluster is access-entries
  only;
- one security group for the nodes (all traffic within the group for EFA and
  node-to-node training traffic, all traffic to and from the EKS cluster
  security group for the kubelet and the control plane);
- the lifecycle script HyperPod runs on every node at creation, placed in the
  regional shared bucket by a custom resource so ``SourceS3Uri`` is a prefix
  GCO owns rather than the CDK asset bucket (and removed before the bucket's
  own emptying on teardown, so the versioned bucket can be deleted);
- the three EKS add-ons that make self-managed nodes work on an Auto Mode
  cluster — ``vpc-cni`` and ``kube-proxy`` (DaemonSets that land only on the
  HyperPod nodes) and ``coredns`` (runs on the Auto Mode nodes and publishes
  the ``kube-dns`` Service those nodes' kube-proxy routes to) — pinned in
  :mod:`gco.stacks.constants`, the CNI's network policy agent following
  ``eks_cluster.network_policy_enforcement``;
- the ``AWS::SageMaker::Cluster`` itself with ``Orchestrator.Eks`` pointing at
  the regional cluster, one instance group per configured group (labels and
  taint from :mod:`gco.hyperpod_config`, deep health checks, an optional
  Training Plan), and automatic node recovery unless the block turns it off;
- exact-key ``CfnOutput``s the CLI (``gco stacks hyperpod status``) and the
  live release validation read.

What this construct does not do, by design: the Kubernetes side (the
``aws-hyperpod`` namespace, the health-monitoring agent, the deep-health-check
and job-auto-restart RBAC, the Kueue flavor and queue, the NVIDIA device
plugin) is shipped by the kubectl-applier manifests and the Helm installer
gated on ``{{HYPERPOD_ENABLED}}``, so it converges and prunes with the rest of
the add-on layer. The regional stack orders the cluster after the convergence
trigger so the base manifests are applying before the first node boots.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any, cast

from aws_cdk import CfnElement, CfnOutput, CfnResource, RemovalPolicy, Stack
from aws_cdk import aws_ec2 as ec2
from aws_cdk import aws_eks as eks_l1
from aws_cdk import aws_iam as iam
from aws_cdk import aws_kms as kms
from aws_cdk import aws_s3 as s3
from aws_cdk import aws_sagemaker as sagemaker
from aws_cdk import custom_resources as cr
from constructs import Construct

from gco.hyperpod_config import (
    HYPERPOD_OUTPUT_CLUSTER_ARN,
    HYPERPOD_OUTPUT_CLUSTER_NAME,
    HYPERPOD_OUTPUT_EXECUTION_ROLE_ARN,
    HYPERPOD_OUTPUT_INSTANCE_GROUPS,
    HYPERPOD_OUTPUT_SECURITY_GROUP_ID,
    cluster_name,
    instance_group_names,
    node_labels,
    node_taints,
)
from gco.stacks.constants import (
    EKS_ADDON_COREDNS,
    EKS_ADDON_VPC_CNI,
    kube_proxy_addon_version,
)
from gco.stacks.nag_suppressions import (
    NagSuppression,
    acknowledge_nag_findings,
    suppress_managed_policy_opt_in,
)

#: The node lifecycle script HyperPod runs at creation (``OnCreate``). The EKS
#: orchestrator does the real bootstrap itself; this script only records that
#: it ran, exactly like the sample ``on_create.sh`` in awsome-distributed-training.
LIFECYCLE_SCRIPT = Path(__file__).resolve().parent / "assets" / "hyperpod" / "on_create.sh"
LIFECYCLE_SCRIPT_NAME = "on_create.sh"
#: Key prefix in the regional shared bucket where the script lives.
LIFECYCLE_PREFIX = "hyperpod/lifecycle"

#: The access entry type that enrolls HyperPod nodes; it takes no access policies.
HYPERPOD_ACCESS_ENTRY_TYPE = "HYPERPOD_LINUX"

#: The AWS-managed policy HyperPod requires on the execution role.
CLUSTER_INSTANCE_ROLE_POLICY = "AmazonSageMakerClusterInstanceRolePolicy"

#: ENI, image-pull and Pod Identity actions the HyperPod EKS prerequisites
#: document for the execution role beyond the managed policy ("IAM role for
#: SageMaker HyperPod", the EKS orchestration policy). The ENI actions are the
#: VPC CNI's (``AmazonEKS_CNI_Policy``): ``aws-node`` on a HyperPod node runs
#: with the node's credentials, and ipamd describes the instance type and
#: attaches secondary interfaces before the first pod gets an address. None of
#: these APIs is resource-scopable in a useful way for a node role (the ENIs
#: and images do not exist at synth time), so they are granted on ``*`` and
#: acknowledged below; ``ec2:CreateTags`` is scoped to network interfaces in
#: :data:`ENI_TAG_ACTIONS`, as the documented policy does.
EKS_NODE_ACTIONS: tuple[str, ...] = (
    "ec2:AssignPrivateIpAddresses",
    "ec2:AttachNetworkInterface",
    "ec2:CreateNetworkInterface",
    "ec2:CreateNetworkInterfacePermission",
    "ec2:DeleteNetworkInterface",
    "ec2:DeleteNetworkInterfacePermission",
    "ec2:DescribeInstances",
    "ec2:DescribeInstanceTypes",
    "ec2:DescribeNetworkInterfaces",
    "ec2:DescribeTags",
    "ec2:DescribeVpcs",
    "ec2:DescribeDhcpOptions",
    "ec2:DescribeSubnets",
    "ec2:DescribeSecurityGroups",
    "ec2:DetachNetworkInterface",
    "ec2:ModifyNetworkInterfaceAttribute",
    "ec2:UnassignPrivateIpAddresses",
    "ecr:BatchCheckLayerAvailability",
    "ecr:BatchGetImage",
    "ecr:GetAuthorizationToken",
    "ecr:GetDownloadUrlForLayer",
    "eks-auth:AssumeRoleForPodIdentity",
)

#: The VPC CNI tags the interfaces it creates (``node.k8s.amazonaws.com/*``);
#: the documented policy grants it on network interfaces only.
ENI_TAG_ACTIONS: tuple[str, ...] = ("ec2:CreateTags",)

#: How the mixed-compute add-ons (``vpc-cni``, ``kube-proxy``, ``coredns``)
#: take over objects that already exist in the cluster: the kubectl-applier
#: renders ``kube-system/amazon-vpc-cni`` (the network policy switch) before
#: the add-on exists, and Auto Mode owns the ``policyendpoints`` CRD the CNI
#: also ships. OVERWRITE lets EKS adopt them; the add-on renders the same
#: network-policy value the applier does, so nothing flips.
_MIXED_COMPUTE_ADDON_RESOLVE_CONFLICTS = "OVERWRITE"

#: The cdk-nag rules that flag an all-traffic (protocol -1) ingress rule
#: regardless of its source; the HyperPod group needs two such rules.
_ALL_TRAFFIC_RULES: tuple[str, ...] = (
    "HIPAA.Security-EC2RestrictedCommonPorts",
    "NIST.800.53.R5-EC2RestrictedCommonPorts",
    "PCI.DSS.321-EC2RestrictedCommonPorts",
)


def _removal_policy(name: str) -> RemovalPolicy:
    return RemovalPolicy.RETAIN if name == "retain" else RemovalPolicy.DESTROY


class HyperpodCluster(Construct):
    """One Region's SageMaker HyperPod cluster, orchestrated by the regional EKS cluster."""

    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        config: Mapping[str, Any],
        project_name: str,
        region: str,
        vpc: ec2.IVpc,
        subnets: Sequence[ec2.ISubnet],
        eks_cluster_name: str,
        eks_cluster_arn: str,
        eks_cluster_security_group: ec2.ISecurityGroup,
        kubernetes_version: str,
        network_policy_enforcement: bool,
        shared_bucket: s3.IBucket,
        shared_bucket_key: kms.IKey,
        custom_resource_role: iam.IRole,
    ) -> None:
        super().__init__(scope, construct_id)
        if not subnets:
            raise ValueError("HyperpodCluster needs at least one subnet")
        self.config = config
        self.project_name = project_name
        self.region = region
        self.vpc = vpc
        self.eks_cluster_security_group = eks_cluster_security_group
        self.kubernetes_version = kubernetes_version
        self.network_policy_enforcement = network_policy_enforcement
        self.removal_policy = _removal_policy(str(config["removal_policy"]))
        self.cluster_name = cluster_name(config, project_name)
        self.subnet = self._select_subnet(subnets)

        self.execution_role = self._create_execution_role(shared_bucket, shared_bucket_key)
        self.access_entry = eks_l1.CfnAccessEntry(
            self,
            "NodeAccessEntry",
            cluster_name=eks_cluster_name,
            principal_arn=self.execution_role.role_arn,
            type=HYPERPOD_ACCESS_ENTRY_TYPE,
        )
        self.security_group = self._create_security_group()
        self.lifecycle_script = self._publish_lifecycle_script(
            shared_bucket, shared_bucket_key, custom_resource_role
        )
        self.addons = self._create_mixed_compute_addons(eks_cluster_name)
        self.cluster = self._create_cluster(eks_cluster_arn, shared_bucket)
        self._outputs()

    # -- mixed-compute add-ons ---------------------------------------------------

    def _create_mixed_compute_addons(self, eks_cluster_name: str) -> dict[str, eks_l1.CfnAddon]:
        """The EKS add-ons HyperPod nodes need on an Auto Mode cluster, by add-on name.

        Auto Mode gives pod networking, kube-proxy and cluster DNS only to the
        nodes it manages; HyperPod's instances are self-managed nodes to EKS,
        so the VPC CNI and kube-proxy DaemonSets (anti-affine to Auto Mode
        nodes, they land only on the HyperPod nodes) and CoreDNS (its pods run
        on the Auto Mode nodes and publish the ``kube-dns`` Service kube-proxy
        on the HyperPod nodes routes to) come from the EKS add-ons. The CNI's
        network policy agent follows ``eks_cluster.network_policy_enforcement``
        so the HyperPod nodes enforce the shipped NetworkPolicies the same way
        the Auto Mode nodes do, and so the add-on renders the same
        ``amazon-vpc-cni`` ConfigMap value the kubectl-applier does.
        """
        kube_proxy_version = kube_proxy_addon_version(self.kubernetes_version)
        enforcement = "true" if self.network_policy_enforcement else "false"
        addons: dict[str, eks_l1.CfnAddon] = {}
        for construct_id, name, version, configuration in (
            (
                "VpcCniAddon",
                "vpc-cni",
                EKS_ADDON_VPC_CNI,
                {"enableNetworkPolicy": enforcement},
            ),
            ("KubeProxyAddon", "kube-proxy", kube_proxy_version, None),
            ("CoreDnsAddon", "coredns", EKS_ADDON_COREDNS, None),
        ):
            addon = eks_l1.CfnAddon(
                self,
                construct_id,
                cluster_name=eks_cluster_name,
                addon_name=name,
                addon_version=version,
                resolve_conflicts=_MIXED_COMPUTE_ADDON_RESOLVE_CONFLICTS,
                preserve_on_delete=False,
                configuration_values=(
                    json.dumps(configuration, sort_keys=True) if configuration else None
                ),
            )
            addons[name] = addon
        return addons

    # -- placement -------------------------------------------------------------

    def _select_subnet(self, subnets: Sequence[ec2.ISubnet]) -> ec2.ISubnet:
        """The one subnet the instance groups launch into.

        HyperPod places a cluster in one Availability Zone (EFA, Capacity
        Blocks and Training Plans are all zonal). ``availability_zone`` picks
        the private subnet in that zone; otherwise the first EKS-supported
        private subnet is used.
        """
        zone = self.config.get("availability_zone")
        if zone is None:
            return subnets[0]
        for subnet in subnets:
            if subnet.availability_zone == zone:
                return subnet
        zones = ", ".join(sorted({str(subnet.availability_zone) for subnet in subnets}))
        raise ValueError(
            f"hyperpod.availability_zone {zone!r} has no private subnet in the regional VPC "
            f"(EKS-supported zones: {zones})"
        )

    # -- IAM -------------------------------------------------------------------

    def _create_execution_role(self, bucket: s3.IBucket, key: kms.IKey) -> iam.Role:
        role = iam.Role(
            self,
            "ExecutionRole",
            assumed_by=iam.ServicePrincipal("sagemaker.amazonaws.com"),
            description=f"SageMaker HyperPod cluster {self.cluster_name}: instance group execution role",
            managed_policies=[
                iam.ManagedPolicy.from_aws_managed_policy_name(CLUSTER_INSTANCE_ROLE_POLICY)
            ],
        )
        role.add_to_policy(
            iam.PolicyStatement(
                sid="EksNodeNetworkingAndImages",
                actions=list(EKS_NODE_ACTIONS),
                resources=["*"],
            )
        )
        stack = Stack.of(self)
        role.add_to_policy(
            iam.PolicyStatement(
                sid="EksNodeInterfaceTags",
                actions=list(ENI_TAG_ACTIONS),
                resources=[
                    f"arn:{stack.partition}:ec2:{stack.region}:{stack.account}:network-interface/*"
                ],
            )
        )
        # Exact statements rather than grant_read(): the recursive copy of the
        # lifecycle prefix needs ListBucket on the bucket and GetObject under
        # the prefix, and nothing wider.
        role.add_to_policy(
            iam.PolicyStatement(
                sid="HyperpodLifecycleScriptRead",
                actions=["s3:GetObject"],
                resources=[bucket.arn_for_objects(f"{LIFECYCLE_PREFIX}/*")],
            )
        )
        role.add_to_policy(
            iam.PolicyStatement(
                sid="HyperpodLifecycleScriptList",
                actions=["s3:ListBucket"],
                resources=[bucket.bucket_arn],
                conditions={"StringLike": {"s3:prefix": [f"{LIFECYCLE_PREFIX}/*"]}},
            )
        )
        role.add_to_policy(
            iam.PolicyStatement(
                sid="HyperpodLifecycleScriptDecrypt",
                actions=["kms:Decrypt"],
                resources=[key.key_arn],
            )
        )
        role.apply_removal_policy(self.removal_policy)
        suppress_managed_policy_opt_in(
            role,
            managed_policy_name=CLUSTER_INSTANCE_ROLE_POLICY,
            reason=(
                "SageMaker HyperPod requires AmazonSageMakerClusterInstanceRolePolicy on the "
                "execution role of every instance group; it is the AWS-maintained statement "
                "of what the HyperPod agents on a node may call."
            ),
        )
        bucket_logical_id = Stack.of(self).get_logical_id(
            cast(CfnElement, bucket.node.default_child)
        )
        acknowledge_nag_findings(
            role,
            [
                NagSuppression(
                    id="AwsSolutions-IAM5",
                    reason=(
                        "The ENI management, image pull and Pod Identity actions HyperPod's "
                        "EKS prerequisites require on the execution role act on network "
                        "interfaces and images that do not exist at synth time; the actions "
                        "are the documented list and nothing more."
                    ),
                    applies_to=["Resource::*"],
                ),
                NagSuppression(
                    id="AwsSolutions-IAM5",
                    reason=(
                        "s3:GetObject under the hyperpod/lifecycle/ prefix of the regional "
                        "shared bucket: the node lifecycle script lives there and nowhere else."
                    ),
                    applies_to=[f"Resource::<{bucket_logical_id}.Arn>/{LIFECYCLE_PREFIX}/*"],
                ),
                NagSuppression(
                    id="AwsSolutions-IAM5",
                    reason=(
                        "ec2:CreateTags on this account's network interfaces in this Region: "
                        "the VPC CNI on each HyperPod node tags the secondary interfaces it "
                        "creates for pod addresses, and their ids do not exist at synth time. "
                        "The same scope as HyperPod's documented EKS execution-role policy."
                    ),
                    applies_to=[
                        "Resource::arn:<AWS::Partition>:ec2:<AWS::Region>:<AWS::AccountId>:"
                        "network-interface/*"
                    ],
                ),
            ],
        )
        return role

    # -- networking -------------------------------------------------------------

    def _create_security_group(self) -> ec2.SecurityGroup:
        """The nodes' security group.

        HyperPod documents the permissive shape within the group (training
        traffic, EFA's SRD protocol, which is neither TCP nor UDP) and EKS
        needs the control plane and the kubelets to talk both ways, so the
        group and the EKS cluster security group admit each other whole.
        """
        security_group = ec2.SecurityGroup(
            self,
            "SecurityGroup",
            vpc=self.vpc,
            security_group_name=f"{self.project_name}-hyperpod-sg-{self.region}",
            description=f"SageMaker HyperPod cluster {self.cluster_name}: instance group nodes",
            allow_all_outbound=True,
        )
        security_group.add_ingress_rule(
            peer=security_group,
            connection=ec2.Port.all_traffic(),
            description="HyperPod nodes to each other: training traffic and EFA",
        )
        security_group.add_ingress_rule(
            peer=self.eks_cluster_security_group,
            connection=ec2.Port.all_traffic(),
            description="EKS control plane and cluster pods to the HyperPod kubelets",
        )
        self.eks_cluster_security_group.add_ingress_rule(
            peer=security_group,
            connection=ec2.Port.all_traffic(),
            description="HyperPod nodes to the EKS control plane and cluster pods",
        )
        security_group.apply_removal_policy(self.removal_policy)
        all_traffic_rules = [
            child
            for child in security_group.node.children
            if isinstance(child, ec2.CfnSecurityGroupIngress) and child.ip_protocol == "-1"
        ]
        for rule in all_traffic_rules:
            acknowledge_nag_findings(
                rule,
                [
                    NagSuppression(
                        id=rule_id,
                        reason=(
                            "The HyperPod node security group admits all traffic from itself "
                            "and from the EKS cluster security group only (security-group "
                            "sources, no CIDR): the AWS-documented shape for HyperPod nodes, "
                            "which carries EFA's SRD protocol and the kubelet/control-plane "
                            "traffic in both directions. The rule packs flag protocol -1 "
                            "regardless of source."
                        ),
                    )
                    for rule_id in _ALL_TRAFFIC_RULES
                ],
            )
        return security_group

    # -- lifecycle script -------------------------------------------------------

    def _publish_lifecycle_script(
        self, bucket: s3.IBucket, key: kms.IKey, role: iam.IRole
    ) -> cr.AwsCustomResource:
        """Put ``on_create.sh`` under ``hyperpod/lifecycle/`` in the regional shared bucket.

        HyperPod's ``SourceS3Uri`` is a prefix it copies whole onto every
        node, so the script cannot sit in the CDK asset bucket next to every
        other asset. The object is written through the stack's shared
        custom-resource role on create and on every update (the body is the
        script's text, so a changed script re-publishes) and removed on delete.
        """
        body = LIFECYCLE_SCRIPT.read_text(encoding="utf-8")
        object_key = f"{LIFECYCLE_PREFIX}/{LIFECYCLE_SCRIPT_NAME}"
        role.add_to_principal_policy(
            iam.PolicyStatement(
                sid="HyperpodLifecycleScript",
                actions=["s3:PutObject", "s3:DeleteObject"],
                resources=[bucket.arn_for_objects(object_key)],
            )
        )
        role.add_to_principal_policy(
            iam.PolicyStatement(
                sid="HyperpodLifecycleScriptKms",
                actions=["kms:GenerateDataKey", "kms:Decrypt"],
                resources=[key.key_arn],
            )
        )

        def call(physical: cr.PhysicalResourceId | None = None) -> cr.AwsSdkCall:
            return cr.AwsSdkCall(
                service="S3",
                action="putObject",
                parameters={
                    "Bucket": bucket.bucket_name,
                    "Key": object_key,
                    "Body": body,
                    "ContentType": "text/x-shellscript",
                    "ServerSideEncryption": "aws:kms",
                    "SSEKMSKeyId": key.key_arn,
                },
                physical_resource_id=physical,
            )

        resource = cr.AwsCustomResource(
            self,
            "LifecycleScript",
            on_create=call(cr.PhysicalResourceId.of(f"{self.cluster_name}-lifecycle-script")),
            on_update=call(),
            on_delete=cr.AwsSdkCall(
                service="S3",
                action="deleteObject",
                parameters={"Bucket": bucket.bucket_name, "Key": object_key},
            ),
            role=role,
        )
        resource.node.add_dependency(role)
        # The bucket is versioned, so that deleteObject writes a delete marker
        # rather than freeing the key — and a bucket holding a marker cannot be
        # deleted. On a stack delete (or a failed first deploy's rollback) the
        # bucket's own auto-delete custom resource empties it, markers
        # included, immediately before the bucket goes; this resource is
        # deleted only after the SageMaker cluster that depends on it, so left
        # to CloudFormation's ordering it wrote its marker *after* the bucket
        # had been emptied, and the bucket's deletion failed (the first live
        # run). Depending on the auto-delete resource makes CloudFormation
        # delete this one first, so the emptying runs last and finds the
        # marker. A retained bucket has no auto-delete resource and is never
        # deleted, so there is nothing to order.
        for child in bucket.node.find_all():
            if (
                isinstance(child, CfnResource)
                and child.cfn_resource_type == "Custom::S3AutoDeleteObjects"
            ):
                resource.node.add_dependency(child)
        return resource

    # -- the cluster -----------------------------------------------------------

    def _instance_group(
        self, group: Mapping[str, Any], bucket: s3.IBucket
    ) -> sagemaker.CfnCluster.ClusterInstanceGroupProperty:
        name = str(group["name"])
        checks = list(group["deep_health_checks"])
        training_plan = group.get("training_plan_arn")
        return sagemaker.CfnCluster.ClusterInstanceGroupProperty(
            instance_group_name=name,
            instance_type=str(group["instance_type"]),
            instance_count=int(group["instance_count"]),
            execution_role=self.execution_role.role_arn,
            threads_per_core=int(group["threads_per_core"]),
            life_cycle_config=sagemaker.CfnCluster.ClusterLifeCycleConfigProperty(
                source_s3_uri=f"s3://{bucket.bucket_name}/{LIFECYCLE_PREFIX}/",
                on_create=LIFECYCLE_SCRIPT_NAME,
            ),
            on_start_deep_health_checks=checks or None,
            training_plan_arn=str(training_plan) if training_plan else None,
            kubernetes_config=sagemaker.CfnCluster.ClusterKubernetesConfigProperty(
                labels=node_labels(name),
                taints=[
                    sagemaker.CfnCluster.ClusterKubernetesTaintProperty(
                        key=taint["key"], value=taint["value"], effect=taint["effect"]
                    )
                    for taint in node_taints()
                ],
            ),
            instance_storage_configs=[
                sagemaker.CfnCluster.ClusterInstanceStorageConfigProperty(
                    ebs_volume_config=sagemaker.CfnCluster.ClusterEbsVolumeConfigProperty(
                        root_volume=True,
                        volume_size_in_gb=int(group["root_volume_size_gb"]),
                    )
                )
            ],
        )

    def _create_cluster(self, eks_cluster_arn: str, bucket: s3.IBucket) -> sagemaker.CfnCluster:
        cluster = sagemaker.CfnCluster(
            self,
            "Cluster",
            cluster_name=self.cluster_name,
            node_recovery=str(self.config["node_recovery"]),
            orchestrator=sagemaker.CfnCluster.OrchestratorProperty(
                eks=sagemaker.CfnCluster.ClusterOrchestratorEksConfigProperty(
                    cluster_arn=eks_cluster_arn
                )
            ),
            vpc_config=sagemaker.CfnCluster.VpcConfigProperty(
                security_group_ids=[self.security_group.security_group_id],
                subnets=[self.subnet.subnet_id],
            ),
            instance_groups=[
                self._instance_group(group, bucket) for group in self.config["instance_groups"]
            ],
            tags=[{"key": key, "value": value} for key, value in sorted(self._tags().items())],
        )
        # Nodes can only join once the access entry exists and the script is
        # in place; the explicit edges keep deletion ordered the same way.
        cluster.node.add_dependency(self.access_entry)
        cluster.node.add_dependency(self.lifecycle_script)
        cluster.node.add_dependency(self.security_group)
        # The CNI and kube-proxy DaemonSets exist before the first node joins,
        # so it turns Ready as soon as the kubelet registers (and, on stack
        # deletion, the nodes are gone before their networking is removed).
        # CoreDNS runs on the Auto Mode nodes and needs no ordering.
        cluster.node.add_dependency(self.addons["vpc-cni"])
        cluster.node.add_dependency(self.addons["kube-proxy"])
        cluster.apply_removal_policy(self.removal_policy)
        return cluster

    def _tags(self) -> dict[str, str]:
        """Tags on the cluster (HyperPod propagates them to the instances it launches)."""
        return {
            "Project": self.project_name,
            "gco:project": self.project_name,
            "gco:hyperpod-cluster": self.cluster_name,
        }

    # -- outputs ---------------------------------------------------------------

    def _output(self, key: str, value: str, description: str) -> CfnOutput:
        """A stack output under exactly ``key`` (the construct is nested; CDK would hash the id)."""
        output = CfnOutput(self, key, value=value, description=description)
        output.override_logical_id(key)
        return output

    def _outputs(self) -> None:
        self._output(
            HYPERPOD_OUTPUT_CLUSTER_ARN,
            self.cluster.attr_cluster_arn,
            "ARN of the SageMaker HyperPod cluster attached to the regional EKS cluster",
        )
        self._output(
            HYPERPOD_OUTPUT_CLUSTER_NAME, self.cluster_name, "SageMaker HyperPod cluster name"
        )
        self._output(
            HYPERPOD_OUTPUT_EXECUTION_ROLE_ARN,
            self.execution_role.role_arn,
            "Execution role of the HyperPod instance groups (HYPERPOD_LINUX access entry)",
        )
        self._output(
            HYPERPOD_OUTPUT_SECURITY_GROUP_ID,
            self.security_group.security_group_id,
            "Security group on the HyperPod nodes",
        )
        self._output(
            HYPERPOD_OUTPUT_INSTANCE_GROUPS,
            json.dumps(instance_group_names(self.config)),
            "HyperPod instance groups (JSON list, declaration order)",
        )
