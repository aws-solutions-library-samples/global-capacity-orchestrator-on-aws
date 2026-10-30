"""AWS PCS for a GCO region: the cluster facts the API needs, and in managed mode the cluster.

What the regional stack needs from PCS, whichever mode ``pcs`` is in:

- The cluster's **identity** (id and ARN) so the API pod's role can be granted
  ``pcs:GetCluster`` on exactly that cluster, and so the manifest-processor
  Deployment learns which cluster to discover at runtime
  (``{{PCS_CLUSTER_ID}}``).
- The **JWT signing key's secret ARN**, which only ``GetCluster`` reports
  (``AWS::PCS::Cluster`` returns ``Arn``, ``Endpoints``, ``Id`` and ``Status``
  but not the Slurm configuration), so the API pod's role can be granted
  ``secretsmanager:GetSecretValue`` on exactly that secret. The
  :class:`PcsClusterFacts` custom resource reads it with a flat response path.
  The SLURMRESTD endpoint is a member of a list the custom resource cannot
  filter by type; the API discovers it at runtime instead
  (:mod:`gco.services.pcs_backend`).

In ``existing`` mode that is all GCO deploys: no PCS resources, two IAM
grants and the NetworkPolicy token for the API pod's egress
(``post-helm-pcs-network.yaml``). The operator keeps the cluster in the
regional VPC or a peered range named in ``pcs.endpoint_cidr``, with its
security group admitting tcp/6820 from the EKS cluster security group, and
with the REST API on (``slurmRest.mode STANDARD``, Slurm 25.11 or 26.05);
``gco stacks pcs preflight`` checks those before a deploy and the API answers
503 with the remedy if they drift afterwards.

In ``managed`` mode the construct also creates the cluster and everything
around it (:meth:`PcsCluster._create_managed`), from the validated
``pcs.managed`` block (:mod:`gco.pcs_config`):

- one **security group** shared by the controller ENI and every node (all
  traffic from itself, as PCS documents; tcp/6820 from the EKS cluster
  security group for the API pod; all outbound, which also carries EFA
  traffic between nodes);
- the **cluster** (Slurm version, size, accounting, the REST API always on,
  idle scale-down, cluster-level custom settings);
- the compute nodes' **instance profile** (role under ``/aws-pcs/``, the
  PCS node policy, SSM for ``gco slurm shell``, reads of the lifecycle
  scripts and the shared storage);
- one **launch template** and one **compute node group** per declared
  group, with the AMI resolved by :class:`PcsLookup` (``sample`` and
  ``dlami``) or passed through (``ami-...``), EFA network interfaces, a
  cluster placement group, capacity reservation targeting, Spot options
  and node-group custom settings;
- the **queues**, with ``Default=YES`` on the one the block marks default;
- an always-on **login node group** reached over SSM;
- the **node lifecycle actions** that mount the regional EFS at ``/home``
  through a PCS-only access point, FSx for Lustre at ``/scratch``, forward
  the lifecycle logs to CloudWatch, and create the API identity's home.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Mapping, Sequence
from pathlib import Path, PurePosixPath
from typing import Any, cast

from aws_cdk import CfnOutput, CfnTag, CustomResource, Duration, RemovalPolicy, Stack, Token
from aws_cdk import aws_ec2 as ec2
from aws_cdk import aws_efs as efs
from aws_cdk import aws_fsx as fsx
from aws_cdk import aws_iam as iam
from aws_cdk import aws_lambda as lambda_
from aws_cdk import aws_logs as logs
from aws_cdk import aws_pcs as pcs
from aws_cdk import aws_s3_assets as s3_assets
from aws_cdk import custom_resources as cr
from constructs import Construct

from gco.pcs_config import (
    LOGIN_NODE_GROUP_NAME,
    PCS_CONTROLLER_PORT,
    PCS_HOME_EFS_PATH,
    PCS_HOME_MOUNT_PATH,
    PCS_OUTPUT_CLUSTER_ID,
    PCS_OUTPUT_CLUSTER_NAME,
    PCS_OUTPUT_LOGIN_NODE_GROUP_ID,
    PCS_OUTPUT_MODE,
    PCS_OUTPUT_NODE_ROLE_ARN,
    PCS_OUTPUT_RESTAPI_MODE,
    PCS_OUTPUT_RESTAPI_PORT,
    PCS_OUTPUT_SECURITY_GROUP_ID,
    PCS_OUTPUT_SLURM_VERSION,
    PCS_RESTAPI_PORT,
    PCS_SCRATCH_MOUNT_PATH,
    is_managed,
    managed_cluster_name,
    managed_slurm_custom_settings,
    metrics_enabled,
    pcs_cluster_arn,
    scratch_backend,
)
from gco.stacks.constants import LAMBDA_PYTHON_RUNTIME
from gco.stacks.lambda_assets import lambda_source_code
from gco.stacks.nag_suppressions import (
    NagSuppression,
    acknowledge_nag_findings,
    suppress_managed_policy_opt_in,
)

#: ``GetCluster`` response members the facts custom resource keeps. Listing
#: them bounds the stored response (CloudFormation caps custom resource data
#: at 4 KiB) and names the flat paths the stack reads.
_FACTS_OUTPUT_PATHS: tuple[str, ...] = (
    "cluster.id",
    "cluster.arn",
    "cluster.name",
    "cluster.status",
    "cluster.scheduler.version",
    "cluster.slurmConfiguration.slurmRest.mode",
    "cluster.slurmConfiguration.accounting.mode",
    "cluster.slurmConfiguration.jwtAuth.jwtKey.secretArn",
    "cluster.slurmConfiguration.jwtAuth.jwtKey.secretVersion",
)

#: The AWS-maintained node lifecycle scripts the managed cluster runs, pinned
#: to an immutable version (the ``-v1-latest`` aliases move). They live in
#: the public ``aws-pcs-repo-<region>`` bucket of the cluster's Region under
#: this prefix; nodes read them through the VPC's S3 gateway endpoint.
PCS_LIFECYCLE_SCRIPTS_PREFIX = "aws-pcs-node-lifecycle-scripts"
PCS_MOUNT_EFS_SCRIPT = "mount-efs-v1.0.0.sh"
PCS_MOUNT_FSX_SCRIPT = "mount-fsx-lustre-v1.0.0.sh"
PCS_CLOUDWATCH_LOGS_SCRIPT = "configure-cloudwatch-logs-v1.0.0.sh"
#: GCO's own lifecycle action, shipped as an S3 asset of the stack.
IDENTITY_HOME_SCRIPT = (
    Path(__file__).resolve().parent / "assets" / "pcs" / "ensure-identity-home.sh"
)
#: Where ``configure-cloudwatch-logs.sh`` forwards to when not told otherwise.
PCS_LIFECYCLE_LOG_GROUP = "/aws/pcs/{cluster_id}/lifecycle"
#: The cdk-nag rules that flag an all-traffic (protocol -1) ingress rule
#: regardless of its source; see :meth:`PcsCluster._create_security_group`.
_SELF_ALL_TRAFFIC_RULES: tuple[str, ...] = (
    "HIPAA.Security-EC2RestrictedCommonPorts",
    "NIST.800.53.R5-EC2RestrictedCommonPorts",
    "PCI.DSS.321-EC2RestrictedCommonPorts",
)

_PASCAL_SPLIT = re.compile(r"[^A-Za-z0-9]+")


def _pascal(name: str) -> str:
    """``gco-compute`` -> ``GcoCompute``: a construct-id fragment from a PCS name."""
    return "".join(part[:1].upper() + part[1:] for part in _PASCAL_SPLIT.split(name) if part)


def _custom_settings(settings: Mapping[str, str], factory: Any) -> list[Any] | None:
    """Render a ``parameter -> value`` mapping as PCS custom-setting properties."""
    if not settings:
        return None
    return [factory(parameter_name=name, parameter_value=value) for name, value in settings.items()]


class PcsClusterFacts(Construct):
    """``pcs:GetCluster`` at deploy time, reduced to the fields the stack wires.

    Runs on create and on every update (the cluster's key may rotate, the
    scheduler version may change) through the stack's shared custom-resource
    role, which must already hold ``pcs:GetCluster`` on the cluster ARN. The
    latest AWS SDK is installed into the provider on first use because the
    runtime's bundled SDK may predate the PCS client (the same live-earned
    posture as the vector-index custom resources in the global stack).
    """

    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        cluster_id: str,
        physical_id: str,
        role: iam.IRole,
    ) -> None:
        super().__init__(scope, construct_id)

        def call(physical: cr.PhysicalResourceId | None = None) -> cr.AwsSdkCall:
            return cr.AwsSdkCall(
                service="PCS",
                action="getCluster",
                parameters={"clusterIdentifier": cluster_id},
                output_paths=list(_FACTS_OUTPUT_PATHS),
                physical_resource_id=physical,
            )

        self.resource = cr.AwsCustomResource(
            self,
            "GetCluster",
            on_create=call(cr.PhysicalResourceId.of(physical_id)),
            on_update=call(),
            install_latest_aws_sdk=True,
            role=role,
        )
        self.resource.node.add_dependency(role)

    def field(self, path: str) -> str:
        """A deploy-time token for one of ``_FACTS_OUTPUT_PATHS``."""
        return str(self.resource.get_response_field(path))

    @property
    def jwt_secret_arn(self) -> str:
        return self.field("cluster.slurmConfiguration.jwtAuth.jwtKey.secretArn")

    @property
    def scheduler_version(self) -> str:
        return self.field("cluster.scheduler.version")

    @property
    def rest_mode(self) -> str:
        return self.field("cluster.slurmConfiguration.slurmRest.mode")


class PcsLookup(Construct):
    """Deploy-time PCS lookups the SDK cannot shape, through one ``cr.Provider``.

    One provider (``lambda/pcs-lookup``) per cluster serving two resource
    types. ``Custom::PcsAmiLookup`` resolves the AWS-published PCS AMIs
    (``sample``, ``dlami``): one custom resource per distinct ``(kind,
    architecture)`` the node groups and the login node ask for, keyed so two
    groups on the same AMI share a lookup. The resource re-runs on every
    deploy (its properties carry the cluster's Slurm version, so a version
    change forces a fresh lookup), which is how a newer AMI release reaches
    the node groups: PCS replaces nodes on the AMI change with its drain
    strategy. ``Custom::PcsControllerLookup`` resolves the controller's
    ``SLURMCTLD`` endpoint (``ip:6817``) by type for the metrics scrape;
    ``GetCluster`` lists endpoints in no guaranteed order, which is why
    ``AwsCustomResource`` cannot do it.
    """

    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        slurm_version: str,
        log_group_removal_policy: RemovalPolicy,
    ) -> None:
        super().__init__(scope, construct_id)
        stack = Stack.of(self)
        self.slurm_version = slurm_version
        self._resources: dict[tuple[str, str], CustomResource] = {}
        #: The ``Custom::PcsControllerLookup`` resource once a caller asks for it.
        self.controller_lookup: CustomResource | None = None

        self.function = lambda_.Function(
            self,
            "Function",
            runtime=getattr(lambda_.Runtime, LAMBDA_PYTHON_RUNTIME),
            handler="handler.lambda_handler",
            code=lambda_source_code("lambda/pcs-lookup"),
            timeout=Duration.minutes(2),
            memory_size=256,
            tracing=lambda_.Tracing.ACTIVE,
            description=(
                "Resolves the AWS PCS sample AMI / PCS-ready DLAMI for a node group and the "
                "cluster controller's address"
            ),
        )
        # describe-images has no resource-level scoping; the public PCS AMI
        # parameters live under one account-less SSM path.
        self.function.add_to_role_policy(
            iam.PolicyStatement(
                sid="DescribePublicAmis",
                effect=iam.Effect.ALLOW,
                actions=["ec2:DescribeImages"],
                resources=["*"],
            )
        )
        self.ssm_parameter_arn = (
            f"arn:{stack.partition}:ssm:{stack.region}::parameter/aws/service/pcs/ami/*"
        )
        self.function.add_to_role_policy(
            iam.PolicyStatement(
                sid="ReadPcsAmiParameters",
                effect=iam.Effect.ALLOW,
                actions=["ssm:GetParameter"],
                resources=[self.ssm_parameter_arn],
            )
        )
        self.log_group = logs.LogGroup(
            self,
            "ProviderLogGroup",
            retention=logs.RetentionDays.ONE_WEEK,
            removal_policy=log_group_removal_policy,
        )
        self.provider = cr.Provider(
            self,
            "Provider",
            on_event_handler=self.function,
            log_group=self.log_group,
        )
        self._acknowledge_findings()

    def image_id(self, *, kind: str, architecture: str, label: str) -> str:
        """The AMI id token for ``kind`` on ``architecture`` (one lookup per pair)."""
        key = (kind, architecture)
        resource = self._resources.get(key)
        if resource is None:
            resource = CustomResource(
                self,
                f"{_pascal(kind)}{_pascal(architecture)}",
                service_token=self.provider.service_token,
                resource_type="Custom::PcsAmiLookup",
                properties={
                    "Kind": kind,
                    "Architecture": architecture,
                    "SlurmVersion": self.slurm_version,
                    "Region": Stack.of(self).region,
                    "NodeGroup": label,
                },
            )
            resource.node.add_dependency(self.log_group)
            self._resources[key] = resource
        # str() for the CDK-less strict mypy job, where get_att_string is Any.
        return str(resource.get_att_string("ImageId"))

    @property
    def lookups(self) -> Mapping[tuple[str, str], CustomResource]:
        return dict(self._resources)

    def controller_address(self, *, cluster_id: str, cluster_arn: str) -> str:
        """The controller's ``ip:6817`` token for the cluster (one lookup per provider).

        The handler needs ``pcs:GetCluster`` on exactly that cluster; the
        grant is written against its ARN (a deploy-time token in managed
        mode) rather than ``*``.
        """
        if self.controller_lookup is None:
            self.function.add_to_role_policy(
                iam.PolicyStatement(
                    sid="DescribePcsCluster",
                    effect=iam.Effect.ALLOW,
                    actions=["pcs:GetCluster"],
                    resources=[cluster_arn],
                )
            )
            self.controller_lookup = CustomResource(
                self,
                "ControllerLookup",
                service_token=self.provider.service_token,
                resource_type="Custom::PcsControllerLookup",
                properties={"ClusterId": cluster_id, "Region": Stack.of(self).region},
            )
            self.controller_lookup.node.add_dependency(self.log_group)
        return str(self.controller_lookup.get_att_string("Address"))

    def _acknowledge_findings(self) -> None:
        acknowledge_nag_findings(
            self.function,
            [
                NagSuppression(
                    id="AwsSolutions-IAM5",
                    reason=(
                        "ec2:DescribeImages has no resource-level permissions, and the "
                        "public PCS AMI parameters live under one account-less SSM path "
                        "(/aws/service/pcs/ami/*) whose leaves name the AMI kind and "
                        "architecture."
                    ),
                    applies_to=["Resource::*", f"Resource::{self.ssm_parameter_arn}"],
                ),
            ],
        )
        function_logical_id = Stack.of(self).get_logical_id(
            cast(lambda_.CfnFunction, self.function.node.default_child)
        )
        acknowledge_nag_findings(
            self.provider,
            [
                NagSuppression(
                    id="AwsSolutions-IAM5",
                    reason=(
                        "The cr.Provider framework role invokes the AMI lookup handler "
                        "through the standard '<lambda-arn>:*' version qualifier, which "
                        "cannot be narrowed at synth time."
                    ),
                    applies_to=[f"Resource::<{function_logical_id}.Arn>:*"],
                ),
            ],
        )


class PcsCluster(Construct):
    """Everything a region deploys for the ``pcs`` block.

    ``api_role`` is the manifest-processor's role: it receives the two reads
    the API pod needs and nothing else. ``custom_resource_role`` is the
    stack's shared ``AwsCustomResource`` execution role. The storage
    arguments and ``provider_log_group_removal_policy`` are consulted in
    managed mode only.
    """

    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        config: Mapping[str, Any],
        project_name: str,
        vpc: ec2.IVpc,
        eks_cluster_security_group: ec2.ISecurityGroup,
        api_role: iam.IRole,
        custom_resource_role: iam.IRole,
        efs_file_system: efs.IFileSystem | None = None,
        efs_security_group: ec2.ISecurityGroup | None = None,
        fsx_file_system: fsx.CfnFileSystem | None = None,
        fsx_security_group: ec2.ISecurityGroup | None = None,
        provider_log_group_removal_policy: RemovalPolicy = RemovalPolicy.DESTROY,
    ) -> None:
        super().__init__(scope, construct_id)
        self.config = dict(config)
        self.project_name = project_name
        self.vpc = vpc
        self.eks_cluster_security_group = eks_cluster_security_group
        self.api_role = api_role
        self.custom_resource_role = custom_resource_role
        self.efs_file_system = efs_file_system
        self.efs_security_group = efs_security_group
        self.fsx_file_system = fsx_file_system
        self.fsx_security_group = fsx_security_group
        self.provider_log_group_removal_policy = provider_log_group_removal_policy
        stack = Stack.of(self)
        self.stack = stack
        self.partition = stack.partition
        self.region = stack.region
        self.account = stack.account
        self.managed = is_managed(self.config)
        #: The cluster id and ARN: literals in existing mode, deploy-time
        #: tokens for the created cluster in managed mode.
        self.cluster_id: str
        self.cluster_arn: str
        #: The ``AWS::PCS::Cluster`` resource in managed mode, else ``None``.
        self.cluster_resource: pcs.CfnCluster | None = None
        #: Managed-mode members, ``None``/empty in existing mode.
        self.cluster_name: str | None = None
        self.security_group: ec2.SecurityGroup | None = None
        self.node_role: iam.Role | None = None
        self.instance_profile: iam.InstanceProfile | None = None
        self.home_access_point: efs.AccessPoint | None = None
        self.identity_home_asset: s3_assets.Asset | None = None
        self.ami_lookup: PcsLookup | None = None
        self.launch_templates: dict[str, ec2.CfnLaunchTemplate] = {}
        self.placement_groups: dict[str, ec2.CfnPlacementGroup] = {}
        self.node_groups: dict[str, pcs.CfnComputeNodeGroup] = {}
        self.queues: dict[str, pcs.CfnQueue] = {}
        self.login_node_group: pcs.CfnComputeNodeGroup | None = None
        self.lifecycle_actions: pcs.CfnComputeNodeGroup.NodeLifecycleActionsProperty | None = None
        self.removal_policy = RemovalPolicy.DESTROY
        self.home_on_efs = False
        self.scratch = "none"
        #: Whether the managed cluster serves Slurm metrics for the in-cluster
        #: Prometheus (``managed.metrics.enabled``); then ``controller_address``
        #: is the ``ip:6817`` token the ScrapeConfig targets, else ``""``.
        self.metrics_enabled = metrics_enabled(self.config)
        self.controller_address = ""

        if self.managed:
            self.cluster_resource = self._create_managed()
        else:
            self.cluster_id = str(self.config["cluster_id"])
            self.cluster_arn = pcs_cluster_arn(
                partition=self.partition,
                region=self.region,
                account=self.account,
                cluster_id=self.cluster_id,
            )

        self._grant_get_cluster(self.custom_resource_role)
        self.facts = PcsClusterFacts(
            self,
            "Facts",
            cluster_id=self.cluster_id,
            physical_id=f"{project_name}-pcs-facts-{self.region}",
            role=self.custom_resource_role,
        )
        if self.cluster_resource is not None:
            self.facts.node.add_dependency(self.cluster_resource)
        self._grant_api_reads()
        self._outputs()

    # -- grants ----------------------------------------------------------------

    def _grant_get_cluster(self, role: iam.IRole) -> None:
        role.add_to_principal_policy(
            iam.PolicyStatement(
                sid="PcsGetCluster",
                effect=iam.Effect.ALLOW,
                actions=["pcs:GetCluster"],
                resources=[self.cluster_arn],
            )
        )

    def _grant_api_reads(self) -> None:
        """The API pod's two reads: the cluster (endpoint discovery) and its JWT key.

        Both are written against exact ARNs. The secret ARN is the one
        ``GetCluster`` reports, so a key the operator rotates to a new secret
        is picked up by the next deploy; a rotation within the same secret
        needs no change at all (the backend reads the version ``GetCluster``
        names). ``kms:Decrypt`` is not granted: PCS creates the secret under
        the AWS-managed Secrets Manager key, and an operator who moves it to a
        customer-managed key grants that key's use alongside.
        """
        self._grant_get_cluster(self.api_role)
        self.api_role.add_to_principal_policy(
            iam.PolicyStatement(
                sid="PcsReadJwtSigningKey",
                effect=iam.Effect.ALLOW,
                actions=["secretsmanager:GetSecretValue", "secretsmanager:DescribeSecret"],
                resources=[self.facts.jwt_secret_arn],
            )
        )

    # -- managed mode ----------------------------------------------------------

    @property
    def managed_config(self) -> dict[str, Any]:
        return cast(dict[str, Any], self.config["managed"])

    @property
    def identity(self) -> dict[str, Any]:
        return cast(dict[str, Any], self.config["identity"])

    def _create_managed(self) -> pcs.CfnCluster:
        """Create the cluster, its node groups, queues, login node and storage wiring."""
        managed = self.managed_config
        self.cluster_name = managed_cluster_name(self.config, self.project_name)
        self.removal_policy = (
            RemovalPolicy.RETAIN if managed["removal_policy"] == "retain" else RemovalPolicy.DESTROY
        )
        self.home_on_efs = managed["storage"]["home"] == "efs"
        self.scratch = scratch_backend(self.config, fsx_enabled=self.fsx_file_system is not None)
        if self.home_on_efs and (self.efs_file_system is None or self.efs_security_group is None):
            raise ValueError(
                "pcs.managed.storage.home is 'efs' but the regional stack has no EFS file system "
                "(and its security group) to mount"
            )
        if self.scratch == "fsx" and (
            self.fsx_file_system is None or self.fsx_security_group is None
        ):
            raise ValueError(
                "pcs.managed.storage.scratch resolves to 'fsx' but the regional stack has no FSx "
                "for Lustre file system; enable fsx_lustre for this region or set scratch to 'none'"
            )

        self.security_group = self._create_security_group()
        cluster = self._create_cluster()
        self.cluster_resource = cluster
        self.cluster_id = cluster.attr_id
        self.cluster_arn = cluster.attr_arn

        if self.home_on_efs:
            self.home_access_point = self._create_home_access_point()
            self.identity_home_asset = s3_assets.Asset(
                self, "IdentityHomeScript", path=str(IDENTITY_HOME_SCRIPT)
            )
        self.node_role, self.instance_profile = self._create_instance_profile()
        self.ami_lookup = PcsLookup(
            self,
            "AmiLookup",
            slurm_version=str(managed["slurm_version"]),
            log_group_removal_policy=self.provider_log_group_removal_policy,
        )
        if self.metrics_enabled:
            # The scrape target for post-helm-pcs-observability.yaml: the
            # controller's slurmctld endpoint, resolved by type once the
            # cluster is ACTIVE (the lookup's property is the cluster id token).
            self.controller_address = self.ami_lookup.controller_address(
                cluster_id=self.cluster_id, cluster_arn=self.cluster_arn
            )
        self.lifecycle_actions = self._lifecycle_actions()

        for group in managed["node_groups"]:
            self.node_groups[group["name"]] = self._create_node_group(group)
        for queue in managed["queues"]:
            self.queues[queue["name"]] = self._create_queue(queue)
        if managed["login"]["enabled"]:
            self.login_node_group = self._create_login_node_group(managed["login"])

        self._open_storage_to_nodes()
        self._managed_outputs()
        return cluster

    # -- networking --

    def _create_security_group(self) -> ec2.SecurityGroup:
        """The one security group on the controller ENI and every node.

        PCS documents the permissive shape: all traffic from the group itself
        (slurmctld <-> slurmd, srun's ephemeral ports, EFA between nodes) and
        all outbound (S3 for the lifecycle scripts, SSM, the EFS and FSx
        mount targets). The API pods reach slurmrestd on tcp/6820 from the
        EKS cluster security group, exactly the rule the existing-mode
        preflight asks an operator for.
        """
        assert self.cluster_name is not None
        security_group = ec2.SecurityGroup(
            self,
            "SecurityGroup",
            vpc=self.vpc,
            security_group_name=f"{self.project_name}-pcs-sg-{self.region}",
            description=f"AWS PCS cluster {self.cluster_name}: controller ENI and compute nodes",
            allow_all_outbound=True,
        )
        security_group.add_ingress_rule(
            peer=security_group,
            connection=ec2.Port.all_traffic(),
            # EC2 rejects "<" and ">" in rule descriptions (the allowed set is
            # a-zA-Z0-9. _-:/()#,@[]+=&;{}!$*); the first live deploy failed here.
            description="Slurm controller to/from compute nodes, srun, and EFA between nodes",
        )
        security_group.add_ingress_rule(
            peer=self.eks_cluster_security_group,
            connection=ec2.Port.tcp(PCS_RESTAPI_PORT),
            description="slurmrestd from the GCO API pods",
        )
        if self.metrics_enabled:
            # PCS documents the metrics prerequisite as "HTTP on 6817 from your
            # desired sources"; the source is Prometheus in the EKS cluster.
            security_group.add_ingress_rule(
                peer=self.eks_cluster_security_group,
                connection=ec2.Port.tcp(PCS_CONTROLLER_PORT),
                description="slurmctld metrics (OpenMetrics) from the in-cluster Prometheus",
            )
        security_group.apply_removal_policy(self.removal_policy)
        # The common-ports rules flag protocol -1 on any ingress rule, whatever
        # its source. This one admits the group itself (a security-group
        # source, no CIDR), and PCS needs it whole: EFA's SRD protocol is
        # neither TCP nor UDP, so it cannot be split into port ranges. The
        # acknowledgment sits on that one rule so a CIDR rule added later is
        # still caught.
        [self_rule] = [
            child
            for child in security_group.node.children
            if isinstance(child, ec2.CfnSecurityGroupIngress) and child.ip_protocol == "-1"
        ]
        acknowledge_nag_findings(
            self_rule,
            [
                NagSuppression(
                    id=rule,
                    reason=(
                        "The PCS security group admits all traffic from itself only (the "
                        "source is the group, not a CIDR): the AWS-documented shape between "
                        "the Slurm controller ENI and the compute nodes, and the only one that "
                        "carries EFA's SRD protocol, which is neither TCP nor UDP. The rule "
                        "packs flag protocol -1 regardless of source."
                    ),
                )
                for rule in _SELF_ALL_TRAFFIC_RULES
            ],
        )
        return security_group

    def _controller_subnet(self) -> ec2.ISubnet:
        """PCS networking takes exactly one subnet; the first private one, like FSx."""
        subnets = self.vpc.private_subnets
        if not subnets:
            raise ValueError("the regional VPC has no private subnets for the PCS controller")
        return subnets[0]

    def _subnet_ids(self, zones: Sequence[str], path: str) -> list[str]:
        """The private subnets a node group launches in, narrowed to ``zones`` when given."""
        subnets = list(self.vpc.private_subnets)
        if not zones:
            return [subnet.subnet_id for subnet in subnets]
        available = [subnet.availability_zone for subnet in subnets]
        if any(Token.is_unresolved(zone) for zone in available):
            raise ValueError(
                f"{path}.availability_zones needs an environment-specific synth (account and "
                "region resolved) so the regional VPC's subnets can be matched by zone"
            )
        missing = sorted(set(zones) - set(available))
        if missing:
            raise ValueError(
                f"{path}.availability_zones names zone(s) without a private subnet in the "
                f"regional VPC: {', '.join(missing)}; the VPC spans {', '.join(available)}"
            )
        return [subnet.subnet_id for subnet in subnets if subnet.availability_zone in zones]

    # -- cluster --

    def _create_cluster(self) -> pcs.CfnCluster:
        managed = self.managed_config
        assert self.security_group is not None
        accounting = managed["accounting"]
        slurm_configuration = pcs.CfnCluster.SlurmConfigurationProperty(
            # The REST API is the whole point: it is what the GCO API fronts.
            slurm_rest=pcs.CfnCluster.SlurmRestProperty(mode="STANDARD"),
            accounting=pcs.CfnCluster.AccountingProperty(
                mode="STANDARD" if accounting["enabled"] else "NONE",
                default_purge_time_in_days=(
                    int(accounting["purge_days"]) if accounting["enabled"] else None
                ),
            ),
            scale_down_idle_time_in_seconds=int(managed["scale_down_idle_seconds"]),
            # With metrics on, the operator's settings plus MetricsType and
            # enable_http (gco.pcs_config.managed_slurm_custom_settings).
            slurm_custom_settings=_custom_settings(
                managed_slurm_custom_settings(self.config),
                pcs.CfnCluster.SlurmCustomSettingProperty,
            ),
            slurmdbd_custom_settings=_custom_settings(
                managed["slurmdbd_custom_settings"], pcs.CfnCluster.SlurmdbdCustomSettingProperty
            ),
            cgroup_custom_settings=_custom_settings(
                managed["cgroup_custom_settings"], pcs.CfnCluster.CgroupCustomSettingProperty
            ),
        )
        cluster = pcs.CfnCluster(
            self,
            "Cluster",
            name=self.cluster_name,
            size=str(managed["size"]),
            scheduler=pcs.CfnCluster.SchedulerProperty(
                type="SLURM", version=str(managed["slurm_version"])
            ),
            networking=pcs.CfnCluster.NetworkingProperty(
                subnet_ids=[self._controller_subnet().subnet_id],
                security_group_ids=[self.security_group.security_group_id],
            ),
            slurm_configuration=slurm_configuration,
            tags=self._tags(),
        )
        cluster.apply_removal_policy(self.removal_policy)
        # The controller ENI needs the group's rules in place before PCS
        # validates them.
        cluster.node.add_dependency(self.security_group)
        return cluster

    def _tags(self, **extra: str) -> dict[str, str]:
        """Tags on every PCS resource and on the instances PCS launches.

        ``gco:project`` is the ownership tag the project's inventory (the live
        release validation's cleanup proof) recognizes on resources
        CloudFormation did not create itself: the EC2 instances and volumes PCS
        launches from the launch templates carry only what the templates'
        tag specifications name.
        """
        assert self.cluster_name is not None
        return {
            "Project": self.project_name,
            "gco:project": self.project_name,
            "gco:pcs-cluster": self.cluster_name,
            **extra,
        }

    # -- storage --

    def _create_home_access_point(self) -> efs.AccessPoint:
        """The PCS-only view of the regional EFS, rooted at ``/pcs/home``.

        The root directory is created owned by the API identity (0755) and no
        POSIX user is enforced, so root on the nodes stays root (the
        lifecycle action creates the identity's home there) and jobs write
        as the identity. The ``gco-jobs`` access point and Slinky's
        ``slurm/home`` directory are elsewhere in the same file system.
        """
        assert self.efs_file_system is not None
        identity = self.identity
        access_point = efs.AccessPoint(
            self,
            "HomeAccessPoint",
            file_system=self.efs_file_system,
            path=PCS_HOME_EFS_PATH,
            create_acl=efs.Acl(
                owner_uid=str(identity["uid"]),
                owner_gid=str(identity["gid"]),
                permissions="755",
            ),
        )
        access_point.apply_removal_policy(self.removal_policy)
        return access_point

    def _open_storage_to_nodes(self) -> None:
        """Let the nodes reach the EFS and FSx mount targets."""
        assert self.security_group is not None
        if self.home_on_efs:
            assert self.efs_security_group is not None
            self.efs_security_group.add_ingress_rule(
                peer=self.security_group,
                connection=ec2.Port.tcp(2049),
                description="NFS from the AWS PCS nodes (/home)",
            )
        if self.scratch == "fsx":
            assert self.fsx_security_group is not None
            self.fsx_security_group.add_ingress_rule(
                peer=self.security_group,
                connection=ec2.Port.tcp(988),
                description="Lustre control traffic from the AWS PCS nodes (/scratch)",
            )
            self.fsx_security_group.add_ingress_rule(
                peer=self.security_group,
                connection=ec2.Port.tcp_range(1018, 1023),
                description="Lustre data traffic from the AWS PCS nodes (/scratch)",
            )

    # -- IAM --

    def _create_instance_profile(self) -> tuple[iam.Role, iam.InstanceProfile]:
        """The compute nodes' role (under ``/aws-pcs/``, as PCS requires) and profile.

        Managed policies: ``AWSPCSComputeNodePolicy`` (the one call a node
        makes to register) and ``AmazonSSMManagedInstanceCore`` (Session
        Manager, for ``gco slurm shell`` and ``gco slurm output``). Inline:
        reads of the AWS-maintained lifecycle scripts and GCO's own one,
        mounting the EFS home through its access point only, and writing
        the lifecycle log group when log forwarding is on. The role's
        policies finish before any node group exists (dependency below), so
        the first node already holds them.
        """
        assert self.cluster_resource is not None
        managed = self.managed_config
        role = iam.Role(
            self,
            "NodeRole",
            path="/aws-pcs/",
            assumed_by=iam.ServicePrincipal("ec2.amazonaws.com"),
            description=f"AWS PCS compute and login nodes of {self.cluster_name}",
            managed_policies=[
                iam.ManagedPolicy.from_aws_managed_policy_name("AWSPCSComputeNodePolicy"),
                iam.ManagedPolicy.from_aws_managed_policy_name("AmazonSSMManagedInstanceCore"),
            ],
        )
        suppress_managed_policy_opt_in(
            role,
            managed_policy_name="AWSPCSComputeNodePolicy",
            reason=(
                "The PCS-published node policy is the single "
                "pcs:RegisterComputeNodeGroupInstance grant every PCS compute node needs; it "
                "is attached only when pcs.mode is managed."
            ),
        )
        suppress_managed_policy_opt_in(
            role,
            managed_policy_name="AmazonSSMManagedInstanceCore",
            reason=(
                "Session Manager is how operators reach the PCS login node (gco slurm shell / "
                "output); the AWS-maintained core policy tracks the SSM agent's requirements "
                "and is attached only when pcs.mode is managed."
            ),
        )

        self.lifecycle_scripts_arn = (
            f"arn:{self.partition}:s3:::aws-pcs-repo-{self.region}/{PCS_LIFECYCLE_SCRIPTS_PREFIX}/*"
        )
        role.add_to_principal_policy(
            iam.PolicyStatement(
                sid="ReadPcsLifecycleScripts",
                effect=iam.Effect.ALLOW,
                actions=["s3:GetObject"],
                resources=[self.lifecycle_scripts_arn],
            )
        )
        acknowledgments = [
            NagSuppression(
                id="AwsSolutions-IAM5",
                reason=(
                    "The AWS-maintained PCS node lifecycle scripts are versioned objects under "
                    "one prefix of the public aws-pcs-repo bucket of this Region; the node reads "
                    "the pinned versions the stack names and nothing else is in the prefix."
                ),
                applies_to=[f"Resource::{self.lifecycle_scripts_arn}"],
            ),
        ]
        if self.identity_home_asset is not None:
            asset = self.identity_home_asset
            role.add_to_principal_policy(
                iam.PolicyStatement(
                    sid="ReadIdentityHomeScript",
                    effect=iam.Effect.ALLOW,
                    actions=["s3:GetObject"],
                    resources=[
                        f"arn:{self.partition}:s3:::{asset.s3_bucket_name}/{asset.s3_object_key}"
                    ],
                )
            )
        if self.home_access_point is not None:
            assert self.efs_file_system is not None
            role.add_to_principal_policy(
                iam.PolicyStatement(
                    sid="MountPcsHome",
                    effect=iam.Effect.ALLOW,
                    actions=[
                        "elasticfilesystem:ClientMount",
                        "elasticfilesystem:ClientWrite",
                        "elasticfilesystem:ClientRootAccess",
                    ],
                    resources=[self.efs_file_system.file_system_arn],
                    conditions={
                        "StringEquals": {
                            "elasticfilesystem:AccessPointArn": (
                                self.home_access_point.access_point_arn
                            )
                        }
                    },
                )
            )
        if managed["lifecycle_logs"]["enabled"]:
            cluster_logical_id = self.stack.get_logical_id(self.cluster_resource)
            log_group = PCS_LIFECYCLE_LOG_GROUP.format(cluster_id=self.cluster_id)
            log_group_arn = (
                f"arn:{self.partition}:logs:{self.region}:{self.account}:log-group:{log_group}"
            )
            role.add_to_principal_policy(
                iam.PolicyStatement(
                    sid="ForwardPcsLifecycleLogs",
                    effect=iam.Effect.ALLOW,
                    actions=[
                        "logs:CreateLogGroup",
                        "logs:CreateLogStream",
                        "logs:PutLogEvents",
                        "logs:DescribeLogStreams",
                    ],
                    resources=[log_group_arn, f"{log_group_arn}:*"],
                )
            )
            placeholder_arn = PCS_LIFECYCLE_LOG_GROUP.format(
                cluster_id=f"<{cluster_logical_id}.Id>"
            )
            acknowledgments.append(
                NagSuppression(
                    id="AwsSolutions-IAM5",
                    reason=(
                        "The CloudWatch agent writes the PCS lifecycle log group of this "
                        "cluster (/aws/pcs/<cluster id>/lifecycle) and its streams; the ':*' "
                        "suffix is the stream qualifier of that one log group."
                    ),
                    applies_to=[
                        f"Resource::arn:<AWS::Partition>:logs:<AWS::Region>:<AWS::AccountId>:"
                        f"log-group:{placeholder_arn}:*"
                    ],
                )
            )
        acknowledge_nag_findings(role, acknowledgments)

        profile = iam.InstanceProfile(self, "NodeInstanceProfile", role=role, path="/aws-pcs/")
        role.apply_removal_policy(self.removal_policy)
        profile.apply_removal_policy(self.removal_policy)
        return role, profile

    # -- node lifecycle actions --

    def _script_uri(self, name: str) -> str:
        return f"s3://aws-pcs-repo-{self.region}/{PCS_LIFECYCLE_SCRIPTS_PREFIX}/{name}"

    def _lifecycle_actions(self) -> pcs.CfnComputeNodeGroup.NodeLifecycleActionsProperty | None:
        """The ``nodeBootstrapped`` scripts every node group (and the login node) runs.

        In order: forward the lifecycle logs (best effort, ``CONTINUE``), mount
        ``/home`` from the EFS access point and ``/scratch`` from FSx
        (``TERMINATE``: a node without its storage must not take jobs), then
        create the API identity's home on the shared ``/home``. All run on
        every boot; the AWS-maintained scripts are idempotent by contract and
        GCO's one by construction. Returns ``None`` when nothing is to run.
        """
        managed = self.managed_config
        script = pcs.CfnComputeNodeGroup.NodeLifecycleScriptProperty
        source = pcs.CfnComputeNodeGroup.ScriptSourceProperty
        scripts: list[Any] = []
        if managed["lifecycle_logs"]["enabled"]:
            # First, so the actions after it are captured from the first
            # boot; the agent configuration it writes survives reboots.
            scripts.append(
                script(
                    name="configure-cloudwatch-logs",
                    script_source=source(
                        script_location=self._script_uri(PCS_CLOUDWATCH_LOGS_SCRIPT)
                    ),
                    execution_policy="FIRST_BOOT_ONLY",
                    on_error="CONTINUE",
                )
            )
        if self.home_on_efs:
            assert self.efs_file_system is not None and self.home_access_point is not None
            scripts.append(
                script(
                    name="mount-efs-home",
                    script_source=source(script_location=self._script_uri(PCS_MOUNT_EFS_SCRIPT)),
                    arguments=[
                        "--file-system-id",
                        self.efs_file_system.file_system_id,
                        "--mount-point",
                        PCS_HOME_MOUNT_PATH,
                        "--options",
                        f"tls,iam,accesspoint={self.home_access_point.access_point_id}",
                    ],
                    execution_policy="EVERY_BOOT",
                    on_error="TERMINATE",
                )
            )
        if self.scratch == "fsx":
            assert self.fsx_file_system is not None
            scripts.append(
                script(
                    name="mount-fsx-scratch",
                    script_source=source(script_location=self._script_uri(PCS_MOUNT_FSX_SCRIPT)),
                    arguments=[
                        "--fsx-dns-name",
                        self.fsx_file_system.attr_dns_name,
                        "--mount-name",
                        self.fsx_file_system.attr_lustre_mount_name,
                        "--mount-point",
                        PCS_SCRATCH_MOUNT_PATH,
                    ],
                    execution_policy="EVERY_BOOT",
                    on_error="TERMINATE",
                )
            )
        if self.identity_home_asset is not None:
            identity = self.identity
            scripts.append(
                script(
                    name="ensure-identity-home",
                    script_source=source(
                        script_location=self.identity_home_asset.s3_object_url,
                        checksum=hashlib.sha256(IDENTITY_HOME_SCRIPT.read_bytes()).hexdigest(),
                    ),
                    arguments=[
                        "--home-base",
                        PCS_HOME_MOUNT_PATH,
                        "--name",
                        PurePosixPath(str(identity["dir"])).name,
                        "--uid",
                        str(identity["uid"]),
                        "--gid",
                        str(identity["gid"]),
                    ],
                    execution_policy="EVERY_BOOT",
                    on_error="TERMINATE",
                )
            )
        if not scripts:
            return None
        return pcs.CfnComputeNodeGroup.NodeLifecycleActionsProperty(
            stages=pcs.CfnComputeNodeGroup.NodeLifecycleStagesProperty(node_bootstrapped=scripts),
            script_caching_policy="CACHE_ONCE",
        )

    # -- node groups --

    def _resolve_ami(self, ami: str, architecture: str, label: str) -> str:
        if ami.startswith("ami-"):
            return ami
        assert self.ami_lookup is not None
        return self.ami_lookup.image_id(kind=ami, architecture=architecture, label=label)

    def _network_interfaces(
        self, efa_interfaces: int
    ) -> list[ec2.CfnLaunchTemplate.NetworkInterfaceProperty]:
        """EFA interfaces for ``efa_interfaces`` network cards, all in the cluster group.

        One card: a single ``efa`` interface (ENA + EFA, the node's IP). More
        cards, as EC2 recommends for multi-card instances: an ENA primary on
        card 0, an ``efa-only`` interface beside it on card 0, and one
        ``efa-only`` interface on each further card, so EFA devices do not
        consume subnet addresses. PCS requires the security groups on every
        interface once interfaces are declared.
        """
        assert self.security_group is not None
        groups = [self.security_group.security_group_id]
        interface = ec2.CfnLaunchTemplate.NetworkInterfaceProperty
        if efa_interfaces == 1:
            return [
                interface(
                    device_index=0,
                    network_card_index=0,
                    interface_type="efa",
                    groups=groups,
                    delete_on_termination=True,
                )
            ]
        interfaces = [
            interface(
                device_index=0,
                network_card_index=0,
                interface_type="interface",
                groups=groups,
                delete_on_termination=True,
            ),
            interface(
                device_index=1,
                network_card_index=0,
                interface_type="efa-only",
                groups=groups,
                delete_on_termination=True,
            ),
        ]
        for card in range(1, efa_interfaces):
            interfaces.append(
                interface(
                    device_index=0,
                    network_card_index=card,
                    interface_type="efa-only",
                    groups=groups,
                    delete_on_termination=True,
                )
            )
        return interfaces

    def _create_launch_template(
        self,
        *,
        name: str,
        efa: bool,
        efa_interfaces: int,
        placement_group: bool,
        capacity_reservation: Mapping[str, str] | None,
        instance_type: str | None,
    ) -> ec2.CfnLaunchTemplate:
        """The launch template PCS launches ``name``'s instances from.

        PCS owns the instance type, AMI (set on the node group), profile and
        subnet; the template carries what PCS cannot set: IMDSv2 only, the
        security group (or EFA interfaces), a cluster placement group, and the
        capacity reservation targeting each purchase option needs: a targeted
        ODCR by id, a Capacity Block with ``MarketType capacity-block`` (and
        the reserved instance type, which Capacity Blocks require in the
        template), an interruptible reservation with its market type.
        """
        assert self.security_group is not None
        pascal = _pascal(name)
        placement = None
        if placement_group:
            group_resource = ec2.CfnPlacementGroup(
                self, f"PlacementGroup{pascal}", strategy="cluster", tags=self._cfn_tags(name)
            )
            group_resource.apply_removal_policy(self.removal_policy)
            self.placement_groups[name] = group_resource
            placement = ec2.CfnLaunchTemplate.PlacementProperty(group_name=group_resource.ref)

        market_options = None
        reservation_spec = None
        if capacity_reservation is not None:
            reservation_spec = ec2.CfnLaunchTemplate.CapacityReservationSpecificationProperty(
                capacity_reservation_target=ec2.CfnLaunchTemplate.CapacityReservationTargetProperty(
                    capacity_reservation_id=capacity_reservation["id"]
                )
            )
            market_type = {
                "capacity_block": "capacity-block",
                "interruptible": "interruptible-capacity-reservation",
            }.get(capacity_reservation["type"])
            if market_type is not None:
                market_options = ec2.CfnLaunchTemplate.InstanceMarketOptionsProperty(
                    market_type=market_type
                )

        data = ec2.CfnLaunchTemplate.LaunchTemplateDataProperty(
            metadata_options=ec2.CfnLaunchTemplate.MetadataOptionsProperty(
                http_endpoint="enabled", http_tokens="required", http_put_response_hop_limit=2
            ),
            security_group_ids=None if efa else [self.security_group.security_group_id],
            network_interfaces=self._network_interfaces(efa_interfaces) if efa else None,
            placement=placement,
            capacity_reservation_specification=reservation_spec,
            instance_market_options=market_options,
            instance_type=instance_type,
            tag_specifications=[
                ec2.CfnLaunchTemplate.TagSpecificationProperty(
                    resource_type="instance", tags=self._cfn_tags(name)
                ),
                ec2.CfnLaunchTemplate.TagSpecificationProperty(
                    resource_type="volume", tags=self._cfn_tags(name)
                ),
            ],
        )
        template = ec2.CfnLaunchTemplate(
            self,
            f"LaunchTemplate{pascal}",
            launch_template_data=data,
            tag_specifications=[
                ec2.CfnLaunchTemplate.LaunchTemplateTagSpecificationProperty(
                    resource_type="launch-template", tags=self._cfn_tags(name)
                )
            ],
        )
        template.apply_removal_policy(self.removal_policy)
        template.node.add_dependency(self.security_group)
        self.launch_templates[name] = template
        return template

    def _cfn_tags(self, node_group: str) -> list[CfnTag]:
        return [
            CfnTag(key=key, value=value)
            for key, value in self._tags(
                Name=f"{self.cluster_name}-{node_group}", **{"gco:pcs-node-group": node_group}
            ).items()
        ]

    def _create_node_group(self, group: Mapping[str, Any]) -> pcs.CfnComputeNodeGroup:
        name = str(group["name"])
        path = f"pcs.managed.node_groups.{name}"
        reservation = group["capacity_reservation"]
        template = self._create_launch_template(
            name=name,
            efa=bool(group["efa"]),
            efa_interfaces=int(group["efa_interfaces"]),
            placement_group=bool(group["placement_group"]),
            capacity_reservation=reservation,
            instance_type=(
                str(group["instance_types"][0])
                if reservation is not None and reservation["type"] == "capacity_block"
                else None
            ),
        )
        purchase = str(group["purchase_option"])
        spot_options = None
        if purchase == "SPOT" and group["spot_allocation_strategy"]:
            spot_options = pcs.CfnComputeNodeGroup.SpotOptionsProperty(
                allocation_strategy=str(group["spot_allocation_strategy"])
            )
        slurm_configuration = None
        if (
            group["slurm_custom_settings"]
            or group["gres_custom_settings"]
            or group["scale_down_idle_seconds"] is not None
        ):
            slurm_configuration = pcs.CfnComputeNodeGroup.SlurmConfigurationProperty(
                slurm_custom_settings=_custom_settings(
                    group["slurm_custom_settings"],
                    pcs.CfnComputeNodeGroup.SlurmCustomSettingProperty,
                ),
                gres_custom_settings=[dict(record) for record in group["gres_custom_settings"]]
                or None,
                scale_down_idle_time_in_seconds=group["scale_down_idle_seconds"],
            )
        return self._node_group(
            name=name,
            ami=self._resolve_ami(str(group["ami"]), str(group["_architecture"]), name),
            template=template,
            instance_types=[str(t) for t in group["instance_types"]],
            min_nodes=int(group["min_nodes"]),
            max_nodes=int(group["max_nodes"]),
            subnet_ids=self._subnet_ids(group["availability_zones"], path),
            purchase_option=purchase,
            spot_options=spot_options,
            slurm_configuration=slurm_configuration,
        )

    def _create_login_node_group(self, login: Mapping[str, Any]) -> pcs.CfnComputeNodeGroup:
        """One always-on On-Demand node in no queue: the SSM target for operators.

        It runs the same lifecycle actions as the compute nodes, so ``/home``
        (and ``/scratch``) look the same from it, which is what lets ``gco
        slurm output`` read a job's ``slurm-<id>.out`` there.
        """
        name = LOGIN_NODE_GROUP_NAME
        template = self._create_launch_template(
            name=name,
            efa=False,
            efa_interfaces=1,
            placement_group=False,
            capacity_reservation=None,
            instance_type=None,
        )
        return self._node_group(
            name=name,
            ami=self._resolve_ami(str(login["ami"]), str(login["_architecture"]), name),
            template=template,
            instance_types=[str(login["instance_type"])],
            min_nodes=1,
            max_nodes=1,
            subnet_ids=self._subnet_ids(login["availability_zones"], "pcs.managed.login"),
            purchase_option="ONDEMAND",
            spot_options=None,
            slurm_configuration=None,
        )

    def _node_group(
        self,
        *,
        name: str,
        ami: str,
        template: ec2.CfnLaunchTemplate,
        instance_types: Sequence[str],
        min_nodes: int,
        max_nodes: int,
        subnet_ids: Sequence[str],
        purchase_option: str,
        spot_options: pcs.CfnComputeNodeGroup.SpotOptionsProperty | None,
        slurm_configuration: pcs.CfnComputeNodeGroup.SlurmConfigurationProperty | None,
    ) -> pcs.CfnComputeNodeGroup:
        assert self.cluster_resource is not None
        assert self.instance_profile is not None and self.node_role is not None
        node_group = pcs.CfnComputeNodeGroup(
            self,
            f"NodeGroup{_pascal(name)}",
            cluster_id=self.cluster_id,
            name=name,
            ami_id=ami,
            custom_launch_template=pcs.CfnComputeNodeGroup.CustomLaunchTemplateProperty(
                template_id=template.ref,
                # The latest version follows every template change, so PCS
                # rolls the nodes (drain strategy) when the stack updates it.
                version=template.attr_latest_version_number,
            ),
            iam_instance_profile_arn=self.instance_profile.instance_profile_arn,
            instance_configs=[
                pcs.CfnComputeNodeGroup.InstanceConfigProperty(instance_type=instance_type)
                for instance_type in instance_types
            ],
            scaling_configuration=pcs.CfnComputeNodeGroup.ScalingConfigurationProperty(
                min_instance_count=min_nodes, max_instance_count=max_nodes
            ),
            subnet_ids=list(subnet_ids),
            purchase_option=purchase_option,
            spot_options=spot_options,
            slurm_configuration=slurm_configuration,
            node_lifecycle_actions=self.lifecycle_actions,
            tags=self._tags(**{"gco:pcs-node-group": name}),
        )
        node_group.apply_removal_policy(self.removal_policy)
        # Nodes launch as soon as the group exists (min_nodes > 0, the login
        # node): the role's policies, the mount targets and the storage
        # security-group rules must all be in place by then.
        node_group.node.add_dependency(self.node_role)
        if self.home_on_efs:
            assert self.efs_file_system is not None and self.efs_security_group is not None
            node_group.node.add_dependency(self.efs_file_system)
            node_group.node.add_dependency(self.efs_security_group)
        if self.scratch == "fsx":
            assert self.fsx_file_system is not None and self.fsx_security_group is not None
            node_group.node.add_dependency(self.fsx_file_system)
            node_group.node.add_dependency(self.fsx_security_group)
        return node_group

    # -- queues --

    def _create_queue(self, queue: Mapping[str, Any]) -> pcs.CfnQueue:
        name = str(queue["name"])
        settings = dict(queue["slurm_custom_settings"])
        if queue["default"]:
            settings["Default"] = "YES"
        slurm_configuration = None
        rendered = _custom_settings(settings, pcs.CfnQueue.SlurmCustomSettingProperty)
        if rendered:
            slurm_configuration = pcs.CfnQueue.SlurmConfigurationProperty(
                slurm_custom_settings=rendered
            )
        resource = pcs.CfnQueue(
            self,
            f"Queue{_pascal(name)}",
            cluster_id=self.cluster_id,
            name=name,
            compute_node_group_configurations=[
                pcs.CfnQueue.ComputeNodeGroupConfigurationProperty(
                    compute_node_group_id=self.node_groups[str(group)].attr_id
                )
                for group in queue["node_groups"]
            ],
            slurm_configuration=slurm_configuration,
            tags=self._tags(),
        )
        resource.apply_removal_policy(self.removal_policy)
        return resource

    # -- outputs ---------------------------------------------------------------

    def _output(self, key: str, value: str, description: str) -> CfnOutput:
        """A stack output under exactly ``key``.

        The construct is nested, so CDK would suffix the logical id with a
        hash; the CLI (``gco slurm shell`` / ``output`` on the PCS backend,
        ``gco stacks pcs status``) reads these by key from ``describe-stacks``.
        """
        output = CfnOutput(self, key, value=value, description=description)
        output.override_logical_id(key)
        return output

    def _managed_outputs(self) -> None:
        assert self.security_group is not None and self.node_role is not None
        self._output(
            PCS_OUTPUT_CLUSTER_NAME,
            str(self.cluster_name),
            "Name of the AWS PCS cluster this stack manages",
        )
        self._output(
            PCS_OUTPUT_SECURITY_GROUP_ID,
            self.security_group.security_group_id,
            "Security group on the PCS controller ENI and every PCS node",
        )
        self._output(
            PCS_OUTPUT_NODE_ROLE_ARN,
            self.node_role.role_arn,
            "IAM role the PCS compute and login nodes run as",
        )
        if self.login_node_group is not None:
            self._output(
                PCS_OUTPUT_LOGIN_NODE_GROUP_ID,
                self.login_node_group.attr_id,
                "PCS compute node group of the login node (gco slurm shell / output)",
            )

    def _outputs(self) -> None:
        self._output(
            PCS_OUTPUT_CLUSTER_ID,
            self.cluster_id,
            f"AWS PCS cluster the {self.region} API fronts ({self.mode})",
        )
        self._output(
            PCS_OUTPUT_MODE, self.mode, "How GCO relates to the PCS cluster: managed or existing"
        )
        self._output(
            PCS_OUTPUT_RESTAPI_PORT,
            str(PCS_RESTAPI_PORT),
            "Port the PCS controller serves slurmrestd on (open it from the EKS SG)",
        )
        # What GetCluster saw at deploy time: the two facts the API's 503
        # remedies are about, visible without a console round trip.
        self._output(
            PCS_OUTPUT_SLURM_VERSION,
            self.facts.scheduler_version,
            "Slurm version of the PCS cluster (25.11 or 26.05 serve the API's v0.0.44)",
        )
        self._output(
            PCS_OUTPUT_RESTAPI_MODE,
            self.facts.rest_mode,
            "slurmRest mode of the PCS cluster (STANDARD is required)",
        )

    @property
    def mode(self) -> str:
        return "managed" if self.managed else "existing"
