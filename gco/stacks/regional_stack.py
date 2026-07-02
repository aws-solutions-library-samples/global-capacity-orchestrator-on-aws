"""
Regional stack for GCO (Global Capacity Orchestrator on AWS) - EKS cluster and ALB per region.

This is the largest stack in the project (~3200 lines) and creates all regional
resources for a single AWS region. One instance is deployed per region defined
in cdk.json.

Resources Created:
    VPC & Networking:
        - VPC with 3 AZs, public subnets (ALB), private subnets (EKS nodes)
        - 2 NAT Gateways for high availability
        - VPC endpoints for ECR, S3, STS, Secrets Manager, SSM, CloudWatch
        - VPC Flow Logs (CloudWatch Logs, 30-day retention)

    EKS Cluster (Auto Mode):
        - Managed control plane with full logging (API, Audit, Authenticator, Controller Manager, Scheduler)
        - NodePools: system, general-purpose, gpu-x86, gpu-arm, inference, gpu-efa, neuron, cpu-general
        - IRSA roles for service accounts (Secrets Manager, SQS, DynamoDB, CloudWatch, S3, EFS)

    Load Balancing:
        - ALB (created by Ingress via AWS Load Balancer Controller)
        - Internal NLB for regional API Gateway VPC Link
        - Global Accelerator endpoint registration (via ga-registration Lambda)

    Storage:
        - EFS with dynamic provisioning (CSI driver, access points, encryption at rest + in transit)
        - FSx for Lustre (optional, toggled via cdk.json)
        - Valkey Serverless cache (optional)
        - Aurora Serverless v2 with pgvector (optional)

    Lambda Functions:
        - kubectl-applier: applies K8s manifests during deployment
        - helm-installer: installs Helm charts (KEDA, Volcano, KubeRay, etc.)
        - ga-registration: registers ALB with Global Accelerator
        - regional-api-proxy: proxies regional API Gateway to internal ALB

    Container Images:
        - ECR repositories + Docker image builds for health-monitor, manifest-processor,
          inference-monitor, queue-processor

    SQS:
        - Regional job queue + dead letter queue (for gco jobs submit-sqs)

Key Design Decisions:
    - EKS Auto Mode handles node provisioning — no managed node groups or Karpenter provisioners
    - NodePools use WhenEmpty consolidation for inference to avoid disrupting long-running pods
    - IRSA (IAM Roles for Service Accounts) for least-privilege pod-level AWS access
    - All optional features (FSx, Valkey, Aurora) are toggled via cdk.json context variables
    - Template variables in K8s manifests ({{PLACEHOLDER}}) are replaced at deploy time

Dependencies:
    - GCOGlobalStack (for Global Accelerator endpoint group ARN, DynamoDB table names, S3 bucket)
    - GCOApiGatewayGlobalStack (for auth secret ARN)

Modification Guide:
    - To add a new NodePool: add a YAML manifest in lambda/kubectl-applier-simple/manifests/ (40-49 range)
    - To add a new service: add ECR image build here, Dockerfile in dockerfiles/, manifest in manifests/
    - To add a new optional feature: add a cdk.json context toggle, guard with if/else in this file
    - To change EKS version: update KUBERNETES_VERSION in constants.py
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import aws_cdk.aws_eks_v2 as eks
import yaml
from aws_cdk import (
    CfnJson,
    CfnOutput,
    CfnTag,
    CustomResource,
    Duration,
    Fn,
    RemovalPolicy,
    Stack,
)
from aws_cdk import aws_ec2 as ec2
from aws_cdk import aws_ecr as ecr
from aws_cdk import aws_ecr_assets as ecr_assets
from aws_cdk import aws_efs as efs
from aws_cdk import aws_eks as eks_l1  # L1 constructs (CfnPodIdentityAssociation)
from aws_cdk import aws_events as events
from aws_cdk import aws_events_targets as events_targets
from aws_cdk import aws_fsx as fsx
from aws_cdk import aws_iam as iam
from aws_cdk import aws_kms as kms
from aws_cdk import aws_lambda as lambda_
from aws_cdk import aws_logs as logs
from aws_cdk import aws_s3 as s3
from aws_cdk import aws_sns as sns
from aws_cdk import aws_sqs as sqs
from aws_cdk import aws_ssm as ssm
from aws_cdk import aws_stepfunctions as sfn
from aws_cdk import aws_stepfunctions_tasks as sfn_tasks
from aws_cdk import custom_resources as cr
from constructs import Construct

from gco.config.config_loader import ConfigLoader
from gco.stacks.constants import (
    AURORA_POSTGRES_VERSION,
    CLUSTER_SHARED_SSM_PARAMETER_PREFIX,
    EKS_ADDON_CLOUDWATCH_OBSERVABILITY,
    EKS_ADDON_EFS_CSI_DRIVER,
    EKS_ADDON_FSX_CSI_DRIVER,
    EKS_ADDON_METRICS_SERVER,
    EKS_ADDON_POD_IDENTITY_AGENT,
    LAMBDA_PYTHON_RUNTIME,
    MOONCAKE_MASTER_DEFAULT_IMAGE,
    REGIONAL_SHARED_BUCKET_NAME_PREFIX,
    REGIONAL_SHARED_SSM_PARAMETER_PREFIX,
)

# <pyflowchart-code-diagram> BEGIN - auto-inserted, do not edit
# Flowchart(s) generated from this file:
#   * ``GCORegionalStack.__init__`` -> ``diagrams/code_diagrams/gco/stacks/regional_stack.GCORegionalStack___init__.html``
#     (PNG: ``diagrams/code_diagrams/gco/stacks/regional_stack.GCORegionalStack___init__.png``)
# Regenerate with ``python diagrams/code_diagrams/generate.py``.
# <pyflowchart-code-diagram> END


@dataclass(frozen=True)
class SharedBucketIdentity:
    """Identity of the always-on ``Cluster_Shared_Bucket`` owned by ``GCOGlobalStack``.

    Every regional stack resolves this identity from the three SSM parameters
    ``/gco/cluster-shared-bucket/{name,arn,region}`` published by
    ``GCOGlobalStack`` in the global region. The three values are used to
    grant IAM permissions on the bucket to the regional job-pod role and to
    populate the ``gco-cluster-shared-bucket`` ConfigMap applied to every
    regional EKS cluster. Frozen so it can be safely shared across helper
    methods without accidental mutation.
    """

    name: str
    arn: str
    region: str


def _compute_kubectl_cluster_shared_replacements(
    shared: SharedBucketIdentity,
) -> dict[str, str]:
    """Build the ``{{CLUSTER_SHARED_BUCKET*}}`` kubectl-applier replacements.

    Pure helper kept at module scope so property and presence tests can
    inspect the output without synthesizing a full regional stack. The
    three keys are always populated — there is no feature toggle — because
    the ``gco-cluster-shared-bucket`` ConfigMap is applied unconditionally
    on every regional cluster.
    """
    return {
        "{{CLUSTER_SHARED_BUCKET}}": shared.name,
        "{{CLUSTER_SHARED_BUCKET_ARN}}": shared.arn,
        "{{CLUSTER_SHARED_BUCKET_REGION}}": shared.region,
    }


def _augment_trusted_registries_with_project_ecr(
    base: list[str],
    *,
    account: str,
    regions: list[str],
    global_region: str,
) -> list[str]:
    """Return the configured trusted registries plus the project's own ECR.

    The new ``gco images build`` flow pushes images to a per-account ECR
    registry under ``<account>.dkr.ecr.<region>.amazonaws.com/gco/<name>``.
    Without this augmentation the queue/manifest validators would treat
    those URIs as untrusted and reject every job that uses one — which
    defeats the whole point of the image registry feature.

    Returns the unique union of the operator-configured ``base`` list
    plus the per-region project ECR hostnames (one per deployed region,
    plus the global region where ``gco-global`` provisions the source
    repo). Order is stable so the rendered ConfigMap doesn't churn
    between deploys.
    """
    augmented: list[str] = list(base)
    seen = set(augmented)
    targets = list(dict.fromkeys([global_region, *regions]))
    if account:
        for region in targets:
            host = f"{account}.dkr.ecr.{region}.amazonaws.com"
            if host not in seen:
                augmented.append(host)
                seen.add(host)
    return augmented


def _load_helm_chart_order() -> list[str]:
    """Return helm chart names in their canonical install order.

    Reads ``lambda/helm-installer/charts.yaml`` (the source of truth, in file
    order) so the Step Functions state machine has exactly one task per chart,
    in the same order every deploy — kueue stays last because its mutating
    webhook intercepts every Job/Deployment. Falls back to an empty list if the
    file can't be read (the synth-time tests assert it is non-empty).
    """
    charts_path = Path(__file__).resolve().parents[2] / "lambda" / "helm-installer" / "charts.yaml"
    try:
        with open(charts_path, encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
    except OSError:
        return []
    charts = data.get("charts", {})
    return list(charts.keys()) if isinstance(charts, dict) else []


class GCORegionalStack(Stack):
    """
    Regional resources stack for a single AWS region.

    Creates EKS cluster, load balancers, and supporting infrastructure
    for running GCO services in a specific region.

    Attributes:
        vpc: VPC with public/private subnets
        cluster: EKS Auto Mode cluster
    """

    @staticmethod
    def _create_irsa_role(
        scope: GCORegionalStack,
        id: str,
        oidc_provider_arn: str,
        oidc_issuer_url: str,
        service_account_names: list[str],
        namespaces: list[str],
    ) -> iam.Role:
        """Create an IAM role trusted by both IRSA (OIDC) and EKS Pod Identity.

        IRSA is the primary credential mechanism — it works reliably on EKS Auto
        Mode by projecting a service-account token that the AWS SDK exchanges for
        temporary credentials via the OIDC provider.

        Pod Identity trust is added as a secondary path so the role is ready if/when
        Pod Identity injection starts working on Auto Mode nodes.

        Uses CfnJson to defer OIDC condition key resolution to deploy time,
        because the issuer URL is a CloudFormation token that can't be used
        as a Python dict key at synth time.
        """
        # Strip https:// from issuer URL for the OIDC condition
        issuer = Fn.select(1, Fn.split("//", oidc_issuer_url))

        # Build OIDC conditions using CfnJson to defer token resolution
        # The issuer URL is a CFN token — can't be used as a dict key at synth time
        aud_key = Fn.join("", [issuer, ":aud"])
        sub_key = Fn.join("", [issuer, ":sub"])

        conditions_json = CfnJson(
            scope,
            f"{id}OidcConditions",
            value={
                aud_key: "sts.amazonaws.com",
                sub_key: [
                    f"system:serviceaccount:{ns}:{sa}"
                    for ns in namespaces
                    for sa in service_account_names
                ],
            },
        )

        role = iam.Role(
            scope,
            id,
            assumed_by=iam.FederatedPrincipal(
                federated=oidc_provider_arn,
                conditions={
                    "StringEquals": conditions_json,
                },
                assume_role_action="sts:AssumeRoleWithWebIdentity",
            ),
        )

        # Also allow Pod Identity (secondary path for future use)
        assert role.assume_role_policy is not None  # guaranteed by assumed_by parameter above
        role.assume_role_policy.add_statements(
            iam.PolicyStatement(
                effect=iam.Effect.ALLOW,
                principals=[iam.ServicePrincipal("pods.eks.amazonaws.com")],
                actions=["sts:AssumeRole", "sts:TagSession"],
            )
        )
        return role

    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        config: ConfigLoader,
        region: str,
        auth_secret_arn: str,
        **kwargs: Any,
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)

        self.config = config
        self.deployment_region = region
        self.auth_secret_arn = auth_secret_arn
        self.alb_arn: str | None = None

        # Get cluster configuration for this region
        cluster_config = self.config.get_cluster_config(region)
        self.cluster_config = cluster_config

        # Create VPC for the EKS cluster
        self.vpc = ec2.Vpc(
            self,
            "GCOVpc",
            # vpc_name intentionally omitted - let CDK generate unique name
            max_azs=3,
            nat_gateways=2,  # For high availability
            subnet_configuration=[
                ec2.SubnetConfiguration(
                    name="PublicSubnet", subnet_type=ec2.SubnetType.PUBLIC, cidr_mask=24
                ),
                ec2.SubnetConfiguration(
                    name="PrivateSubnet",
                    subnet_type=ec2.SubnetType.PRIVATE_WITH_EGRESS,
                    cidr_mask=24,
                ),
            ],
        )

        # Enable VPC Flow Logs for network traffic analysis and security monitoring
        self._create_vpc_flow_logs()

        # Create SQS queue for job ingestion
        self._create_sqs_queue()

        # Create ECR repositories and build Docker images
        self._create_container_images()

        # Pre-create the execution role shared by every ``cr.AwsCustomResource``
        # in this stack. See ``_create_aws_custom_resource_role`` for the full
        # rationale — in short, CDK's default behavior of auto-generating a
        # Lambda role per ``AwsCustomResource`` (and then merging all the
        # ``policy=`` statements onto it during deploy) triggers an IAM
        # propagation race on cold creates. We sidestep the race by creating
        # a single long-lived role up front and attaching policies to it as
        # each consumer is built; every ``AwsCustomResource`` then passes
        # ``role=self.aws_custom_resource_role`` instead of ``policy=``, so
        # the singleton Lambda runs against a role whose inline policy has
        # already replicated globally.
        self._create_aws_custom_resource_role()

        # Create EKS cluster
        self._create_eks_cluster(cluster_config)

        # Optional Volcano image mirror (cdk.json ``volcano_image_mirror``).
        # When enabled this resolves ``self.volcano_mirror_registry`` — the
        # gco/* ECR namespace that Volcano's ``basic.image_registry`` is
        # redirected to, so its docker.io-only images are pulled from the
        # project's own ECR (populated out-of-band by
        # ``gco images mirror``) instead of rate-limited Docker
        # Hub. Creates no CloudFormation resources; must run before
        # ``_apply_kubernetes_manifests`` builds the ``HelmInstallCharts`` custom
        # resource, which reads the override via ``_helm_chart_value_overrides()``.
        self._configure_volcano_image_mirror()

        # Resolve the always-on Cluster_Shared_Bucket identity from SSM
        # (owned by GCOGlobalStack) and attach RW + KMS grants to the
        # job-pod role. Runs unconditionally — the ConfigMap and IAM
        # statements are always present on every regional cluster. Must
        # run after
        # _create_pod_identity_associations (which created service_account_role)
        # and before _apply_kubernetes_manifests (which consumes the
        # replacements in the KubectlApplyManifests CustomResource).
        self.cluster_shared_identity = self._resolve_cluster_shared_bucket_from_ssm()
        self._grant_cluster_shared_bucket_to_job_role(self.cluster_shared_identity)

        # Create the always-on general-purpose regional bucket (KMS key +
        # access-logs bucket + primary bucket). Provisioned unconditionally —
        # there is no cdk.json toggle and no feature flag gating its existence —
        # in addition to the central buckets owned by GCOGlobalStack.
        self._create_regional_shared_bucket()

        # Create EFS for shared storage
        self._create_efs()

        # Create FSx for Lustre (if enabled) for high-performance storage
        self._create_fsx_lustre()

        # Create Valkey Serverless cache (if enabled) for K/V caching
        self._create_valkey_cache()

        # Create Aurora Serverless v2 + pgvector (if enabled) for vector DB
        self._create_aurora_pgvector()

        # Create GA registration Lambda for registering Ingress-created ALB
        self._create_ga_registration_lambda()

        # Create Helm installer Lambda for KEDA and other Helm-based installations
        self._create_helm_installer_lambda()

        # Apply Kubernetes manifests (after EFS so IDs are available)
        self._apply_kubernetes_manifests()

        # Create CloudFormation drift detection (daily schedule + SNS alerts)
        self._create_drift_detection()

        # Create dedicated IAM role for MCP server
        self._create_mcp_role()

        # Export cluster information
        self._create_outputs()

        # Apply cdk-nag suppressions for this stack
        self._apply_nag_suppressions()

    def _create_vpc_flow_logs(self) -> None:
        """Create VPC Flow Logs for network traffic monitoring.

        Flow logs capture information about IP traffic going to and from
        network interfaces in the VPC. This is required for security
        monitoring and compliance (HIPAA, SOC2, etc.).
        """
        # Create CloudWatch Log Group for flow logs
        flow_log_group = logs.LogGroup(
            self,
            "VpcFlowLogGroup",
            # log_group_name intentionally omitted - let CDK generate unique name
            retention=logs.RetentionDays.ONE_MONTH,
            removal_policy=RemovalPolicy.DESTROY,
        )

        # Create IAM role for VPC Flow Logs
        flow_log_role = iam.Role(
            self,
            "VpcFlowLogRole",
            assumed_by=iam.ServicePrincipal("vpc-flow-logs.amazonaws.com"),
        )

        flow_log_role.add_to_policy(
            iam.PolicyStatement(
                actions=[
                    "logs:CreateLogStream",
                    "logs:PutLogEvents",
                    "logs:DescribeLogGroups",
                    "logs:DescribeLogStreams",
                ],
                resources=[flow_log_group.log_group_arn, f"{flow_log_group.log_group_arn}:*"],
            )
        )

        # Create VPC Flow Log
        ec2.FlowLog(
            self,
            "VpcFlowLog",
            resource_type=ec2.FlowLogResourceType.from_vpc(self.vpc),
            destination=ec2.FlowLogDestination.to_cloud_watch_logs(flow_log_group, flow_log_role),
            traffic_type=ec2.FlowLogTrafficType.ALL,
        )

    def _apply_nag_suppressions(self) -> None:
        """Apply cdk-nag suppressions for this stack."""
        from gco.stacks.nag_suppressions import apply_all_suppressions

        apply_all_suppressions(
            self,
            stack_type="regional",
            regions=self.config.get_regions(),
            global_region=self.config.get_global_region(),
        )

    def _create_sqs_queue(self) -> None:
        """Create SQS queue for job ingestion.

        Creates an SQS queue that serves as the default job ingestion point
        for this region. Jobs submitted to this queue are processed by the
        manifest processor and KEDA scales based on queue depth.

        Also creates a dead-letter queue for failed messages.
        Both queues use server-side encryption with AWS managed keys.
        """
        project_name = self.config.get_project_name()

        # Create dead-letter queue for failed messages
        self.job_dlq = sqs.Queue(
            self,
            "JobDeadLetterQueue",
            queue_name=f"{project_name}-jobs-dlq-{self.deployment_region}",
            retention_period=Duration.days(14),
            removal_policy=RemovalPolicy.DESTROY,
            enforce_ssl=True,  # Require SSL for all requests
            encryption=sqs.QueueEncryption.SQS_MANAGED,  # Server-side encryption
        )

        # Create main job queue
        self.job_queue = sqs.Queue(
            self,
            "JobQueue",
            queue_name=f"{project_name}-jobs-{self.deployment_region}",
            visibility_timeout=Duration.minutes(5),  # Match Lambda timeout
            retention_period=Duration.days(7),
            dead_letter_queue=sqs.DeadLetterQueue(
                max_receive_count=3,  # Move to DLQ after 3 failed attempts
                queue=self.job_dlq,
            ),
            removal_policy=RemovalPolicy.DESTROY,
            enforce_ssl=True,  # Require SSL for all requests
            encryption=sqs.QueueEncryption.SQS_MANAGED,  # Server-side encryption
        )

        # Output queue information
        CfnOutput(
            self,
            "JobQueueUrl",
            value=self.job_queue.queue_url,
            description=f"SQS Job Queue URL for {self.deployment_region}",
            export_name=f"{project_name}-job-queue-url-{self.deployment_region}",
        )

        CfnOutput(
            self,
            "JobQueueArn",
            value=self.job_queue.queue_arn,
            description=f"SQS Job Queue ARN for {self.deployment_region}",
            export_name=f"{project_name}-job-queue-arn-{self.deployment_region}",
        )

        CfnOutput(
            self,
            "JobDlqUrl",
            value=self.job_dlq.queue_url,
            description=f"SQS Dead Letter Queue URL for {self.deployment_region}",
            export_name=f"{project_name}-job-dlq-url-{self.deployment_region}",
        )

    def _create_aws_custom_resource_role(self) -> None:
        """Pre-create the execution role shared by every ``AwsCustomResource``.

        CDK's ``cr.AwsCustomResource`` defaults to auto-generating a per-
        construct Lambda execution role from the ``policy=`` parameter.
        Internally, CDK deduplicates those auto-generated roles onto a
        single *singleton* provider Lambda (logical id prefix
        ``AWS679f53fac002430cb0da5b7982bd22872``), and merges each custom
        resource's policy statements onto that Lambda's role at stack
        create time. On cold deploys, CloudFormation invokes the Lambda
        within 2-3 seconds of attaching a new policy statement, which is
        faster than IAM's global propagation window. The symptom is a
        ``iam:PassRole NOT authorized`` failure on whichever addon role
        update happens to run right after its ``iam:PassRole`` policy
        statement was attached but before it had replicated.

        The fix is to create the role up front, attach every policy
        statement the stack will need during stack creation, and pass
        ``role=self.aws_custom_resource_role`` to every
        ``AwsCustomResource`` instead of ``policy=``. Because the role
        already exists — and its inline policy has had minutes to
        replicate by the time any ``AwsCustomResource`` actually fires —
        the race disappears entirely.

        This method creates the role with the statements we can compute
        without a cluster reference (EKS ``UpdateAddon`` / ``DescribeAddon``
        scoped to this cluster, and SSM ``GetParameter`` for the endpoint
        group ARN). ``iam:PassRole`` statements for individual addon
        roles (EFS CSI, FSx CSI, CloudWatch Observability) are appended
        by each ``_create_*_addon`` method after the corresponding IRSA
        role has been created, so every PassRole ``resources=`` list
        stays precise (no wildcards) and cdk-nag stays happy.
        """
        project_name = self.config.get_project_name()
        global_region = self.config.get_global_region()

        self.aws_custom_resource_role = iam.Role(
            self,
            "AwsCustomResourceRole",
            assumed_by=iam.ServicePrincipal("lambda.amazonaws.com"),
            description=(
                "Shared execution role for every cr.AwsCustomResource in this "
                "stack. Pre-created to avoid the IAM policy propagation race "
                "that occurs when CDK auto-generates per-CR roles and the "
                "singleton provider Lambda fires before the freshly-attached "
                "policy has replicated globally."
            ),
            managed_policies=[
                iam.ManagedPolicy.from_aws_managed_policy_name(
                    "service-role/AWSLambdaBasicExecutionRole"
                ),
            ],
        )

        # EKS UpdateAddon / DescribeAddon — used by the three updateAddon
        # custom resources (EFS CSI, FSx CSI, CloudWatch Observability).
        # Scoped to this cluster's addons by ARN.
        self.aws_custom_resource_role.add_to_policy(
            iam.PolicyStatement(
                effect=iam.Effect.ALLOW,
                actions=["eks:UpdateAddon", "eks:DescribeAddon"],
                resources=[
                    f"arn:aws:eks:{self.deployment_region}:{self.account}"
                    f":addon/{self.cluster_config.cluster_name}/*"
                ],
            )
        )

        # SSM GetParameter — used by the GetEndpointGroupArn custom
        # resource in _create_ga_registration_lambda to read the ARN of
        # the Global Accelerator endpoint group published by the global
        # stack during its deploy.
        self.aws_custom_resource_role.add_to_policy(
            iam.PolicyStatement(
                effect=iam.Effect.ALLOW,
                actions=["ssm:GetParameter"],
                resources=[
                    f"arn:aws:ssm:{global_region}:{self.account}:parameter/{project_name}/*"
                ],
            )
        )

        # cdk-nag suppressions: the two wildcard-bearing ARNs above are
        # intentional and both scoped as tightly as AWS IAM permits.
        #
        # - The ``eks:UpdateAddon`` / ``eks:DescribeAddon`` statement uses
        #   ``addon/<cluster>/*`` as its resource because the same shared
        #   role is consumed by three different updateAddon custom
        #   resources (EFS CSI, FSx CSI, CloudWatch Observability). Each
        #   addon has its own ARN and we'd otherwise need three separate
        #   statements that each grant access to a known addon name. The
        #   wildcard is scoped to a single cluster in a single region in
        #   a single account — it cannot be used against any addon
        #   belonging to a different cluster or a different service.
        #
        # - The ``ssm:GetParameter`` statement uses
        #   ``parameter/<project>/*`` because the exact parameter name
        #   (``endpoint-group-<region>-arn``) is only known at Global
        #   Accelerator registration time and the endpoint path
        #   structure is ``<project>/<parameter>``. Scoping to the
        #   project prefix restricts access to parameters owned by this
        #   project only.
        from gco.stacks.nag_suppressions import acknowledge_nag_findings

        acknowledge_nag_findings(
            self.aws_custom_resource_role,
            [
                {
                    "id": "AwsSolutions-IAM5",
                    "reason": (
                        "Scoped to a single EKS cluster's addons "
                        "(addon/<cluster>/*) and this project's SSM "
                        "parameters (parameter/<project>/*). Both wildcards "
                        "are as tight as AWS IAM permits: addon names and "
                        "parameter names are not known at stack synthesis "
                        "time because the addons are created later in the "
                        "same stack and the GA endpoint group ARN is "
                        "published by a separate stack during deploy. The "
                        "shared role pattern itself is deliberate — see "
                        "_create_aws_custom_resource_role docstring for why "
                        "we pre-create instead of letting CDK auto-generate "
                        "per-CR roles."
                    ),
                    "appliesTo": [
                        f"Resource::arn:aws:eks:{self.deployment_region}"
                        f":<AWS::AccountId>:addon/{self.cluster_config.cluster_name}/*",
                        f"Resource::arn:aws:ssm:{global_region}"
                        f":<AWS::AccountId>:parameter/{project_name}/*",
                    ],
                },
            ],
        )

    def _create_container_images(self) -> None:
        """Create ECR repositories and build Docker images for services"""

        # Create ECR repository for health monitor
        self.health_monitor_repo = ecr.Repository(
            self,
            "HealthMonitorRepo",
            # repository_name intentionally omitted - let CDK generate unique name
            removal_policy=RemovalPolicy.DESTROY,  # For dev/test; use RETAIN for production
            empty_on_delete=True,  # Clean up images on stack deletion
            image_scan_on_push=True,  # Enable vulnerability scanning on push
        )

        # All Docker images target AMD64 (x86_64) to match EKS Auto Mode's
        # default system nodepool.

        # Build and push health monitor Docker image
        self.health_monitor_image = ecr_assets.DockerImageAsset(
            self,
            "HealthMonitorImage",
            directory=".",  # Root directory
            file="dockerfiles/health-monitor-dockerfile",
            platform=ecr_assets.Platform.LINUX_AMD64,
        )

        # Create ECR repository for manifest processor
        self.manifest_processor_repo = ecr.Repository(
            self,
            "ManifestProcessorRepo",
            # repository_name intentionally omitted - let CDK generate unique name
            removal_policy=RemovalPolicy.DESTROY,
            empty_on_delete=True,
            image_scan_on_push=True,  # Enable vulnerability scanning on push
        )

        # Build and push manifest processor Docker image
        self.manifest_processor_image = ecr_assets.DockerImageAsset(
            self,
            "ManifestProcessorImage",
            directory=".",
            file="dockerfiles/manifest-processor-dockerfile",
            platform=ecr_assets.Platform.LINUX_AMD64,
        )

        # Output image URIs for reference
        CfnOutput(
            self,
            "HealthMonitorImageUri",
            value=self.health_monitor_image.image_uri,
            description="Health Monitor Docker image URI",
        )

        CfnOutput(
            self,
            "ManifestProcessorImageUri",
            value=self.manifest_processor_image.image_uri,
            description="Manifest Processor Docker image URI",
        )

        # Build and push inference monitor Docker image
        self.inference_monitor_image = ecr_assets.DockerImageAsset(
            self,
            "InferenceMonitorImage",
            directory=".",
            file="dockerfiles/inference-monitor-dockerfile",
            platform=ecr_assets.Platform.LINUX_AMD64,
        )

        CfnOutput(
            self,
            "InferenceMonitorImageUri",
            value=self.inference_monitor_image.image_uri,
            description="Inference Monitor Docker image URI",
        )

        # Build and push queue processor Docker image (if enabled).
        # The queue processor is a KEDA ScaledJob that consumes manifests from
        # the regional SQS queue. It can be disabled in cdk.json if users want
        # to implement their own consumer. When disabled, the post-helm-sqs-consumer.yaml
        # manifest is skipped (unreplaced template variables cause it to be skipped).
        queue_processor_config = self.node.try_get_context("queue_processor") or {}
        self.queue_processor_enabled = queue_processor_config.get("enabled", True)

        if self.queue_processor_enabled:
            self.queue_processor_image = ecr_assets.DockerImageAsset(
                self,
                "QueueProcessorImage",
                directory=".",
                file="dockerfiles/queue-processor-dockerfile",
                platform=ecr_assets.Platform.LINUX_AMD64,
            )

            CfnOutput(
                self,
                "QueueProcessorImageUri",
                value=self.queue_processor_image.image_uri,
                description="Queue Processor Docker image URI",
            )

    def _create_eks_cluster(self, cluster_config: Any) -> None:
        """Create the EKS cluster with auto mode and GPU node groups"""

        # Create cluster admin role
        # role_name intentionally omitted - let CDK generate unique name
        cluster_admin_role = iam.Role(
            self,
            "ClusterAdminRole",
            assumed_by=iam.ServicePrincipal("eks.amazonaws.com"),
            managed_policies=[
                iam.ManagedPolicy.from_aws_managed_policy_name("AmazonEKSClusterPolicy")
            ],
        )

        # Create node group role
        # role_name intentionally omitted - let CDK generate unique name
        iam.Role(
            self,
            "NodeGroupRole",
            assumed_by=iam.ServicePrincipal("ec2.amazonaws.com"),
            managed_policies=[
                iam.ManagedPolicy.from_aws_managed_policy_name("AmazonEKSWorkerNodePolicy"),
                iam.ManagedPolicy.from_aws_managed_policy_name("AmazonEKS_CNI_Policy"),
                iam.ManagedPolicy.from_aws_managed_policy_name(
                    "AmazonEC2ContainerRegistryReadOnly"
                ),
            ],
        )

        # Create EKS Auto Mode cluster with built-in system and general-purpose nodepools
        # Auto Mode automatically manages compute resources and comes with essential addons
        # Get endpoint access configuration
        eks_config = self.config.get_eks_cluster_config()
        endpoint_access_mode = eks_config.get("endpoint_access", "PRIVATE")

        # Map config string to EKS EndpointAccess enum
        endpoint_access = (
            eks.EndpointAccess.PRIVATE
            if endpoint_access_mode == "PRIVATE"
            else eks.EndpointAccess.PUBLIC_AND_PRIVATE
        )

        # Create KMS key for EKS secrets encryption
        self.eks_encryption_key = kms.Key(
            self,
            "EksSecretsEncryptionKey",
            description="KMS key for EKS Kubernetes secrets encryption",
            enable_key_rotation=True,
            removal_policy=RemovalPolicy.RETAIN,
        )

        # Get Kubernetes version - use custom version if not available in CDK enum
        k8s_version_str = cluster_config.kubernetes_version
        try:
            k8s_version = getattr(eks.KubernetesVersion, f"V{k8s_version_str.replace('.', '_')}")
        except AttributeError:
            # Version not in CDK enum yet, use custom version
            k8s_version = eks.KubernetesVersion.of(k8s_version_str)

        self.cluster = eks.Cluster(
            self,
            "GCOEksCluster",
            cluster_name=cluster_config.cluster_name,
            version=k8s_version,  # Use configured version for Auto Mode with DRA support
            vpc=self.vpc,
            compute=eks.ComputeConfig(
                # Enable both built-in node pools - Auto Mode manages these automatically
                node_pools=["system", "general-purpose"]
            ),
            # SECURITY: Endpoint access controlled via cdk.json eks_cluster.endpoint_access
            # PRIVATE (default): EKS API accessible only from within VPC - most secure
            #   Job submission works via API Gateway → Lambda (in VPC) or SQS
            #   For kubectl access, use a bastion host, VPN, or AWS SSM Session Manager
            # PUBLIC_AND_PRIVATE: EKS API accessible from internet and VPC
            #   Allows direct kubectl access but less secure
            endpoint_access=endpoint_access,
            role=cluster_admin_role,
            vpc_subnets=[ec2.SubnetSelection(subnet_type=ec2.SubnetType.PRIVATE_WITH_EGRESS)],
            # Enable all control plane logging for security and compliance
            cluster_logging=[
                eks.ClusterLoggingTypes.API,
                eks.ClusterLoggingTypes.AUDIT,
                eks.ClusterLoggingTypes.AUTHENTICATOR,
                eks.ClusterLoggingTypes.CONTROLLER_MANAGER,
                eks.ClusterLoggingTypes.SCHEDULER,
            ],
            # SECURITY: Enable envelope encryption for Kubernetes secrets using KMS
            secrets_encryption_key=self.eks_encryption_key,
        )

        # The EKS cluster security group's auto-generated ingress rule allows
        # 443 from the VPC CIDR, expressed as a CloudFormation token. cdk-nag's
        # SG-ingress rules can't resolve the token and throw; scope the
        # acknowledgment to the cluster construct so it can't mask an
        # open-ingress finding elsewhere in the stack.
        from gco.stacks.nag_suppressions import acknowledge_security_group_cidr_findings

        acknowledge_security_group_cidr_findings(
            self.cluster,
            reason=(
                "The EKS Auto Mode cluster security group allows HTTPS (443) "
                "ingress from the VPC CIDR only, referenced via an "
                "``Fn::GetAtt`` token that cdk-nag cannot resolve at synth "
                "time. Ingress is restricted to intra-VPC traffic, the "
                "tightest possible source for the Kubernetes API and webhook "
                "endpoints."
            ),
        )

        # Auto Mode comes with essential addons pre-configured:
        # - AWS Load Balancer Controller (for ALB/NLB integration)
        # - CoreDNS, kube-proxy, VPC CNI (standard Kubernetes components)

        # OIDC provider for IRSA — the primary credential injection mechanism.
        # IRSA uses projected service-account tokens exchanged via the OIDC provider
        # for temporary AWS credentials.  This works reliably on EKS Auto Mode.
        self.oidc_provider = eks.OidcProviderNative(
            self,
            "OidcProvider",
            url=self.cluster.cluster_open_id_connect_issuer_url,
        )

        # Pod Identity Agent add-on — registers the admission webhook that injects
        # Pod Identity credentials.  On Auto Mode the DaemonSet schedules 0 pods
        # (the agent is built into the node), but the add-on registration is still
        # needed for the control-plane webhook.  Kept as a secondary credential path.
        self._create_pod_identity_agent_addon()

        # Add Metrics Server add-on for HPA and resource monitoring
        self._create_metrics_server_addon()

        # Add EFS CSI Driver add-on for shared storage
        self._create_efs_csi_driver_addon()

        # Add CloudWatch Observability add-on for Container Insights metrics
        self._create_cloudwatch_observability_addon()

        # NOTE: GPU compute is configured via Karpenter NodePools (not managed node groups)
        # NodePool manifests are located in lambda/kubectl-applier-simple/manifests/:
        # - 40-nodepool-gpu-x86.yaml: x86_64 GPU instances (g4dn, g5, g6, g6e, p3)
        # - 41-nodepool-gpu-arm.yaml: ARM64 GPU instances (g5g)
        # - 42-nodepool-inference.yaml: inference-optimized GPU instances
        # - 43-nodepool-efa.yaml: EFA-enabled instances (p4d, p5/p5e/p5en, p6-b200/p6-b300/p6e-gb200)
        # - 44-nodepool-neuron.yaml: Trainium/Inferentia instances
        # These will be applied by the kubectl Lambda custom resource (created below)

        # Create IRSA role for service account to access secrets
        self._create_service_account_role()

        # Create kubectl Lambda for applying Kubernetes manifests
        self._create_kubectl_lambda()

    # ── Shared toleration config for EKS add-ons ──────────────────────────
    # All GCO nodepools apply taints (nvidia.com/gpu, aws.amazon.com/neuron,
    # vpc.amazonaws.com/efa) that prevent DaemonSet pods from scheduling.
    # Every add-on that runs a DaemonSet (or may schedule on tainted nodes)
    # must tolerate these taints so that storage drivers, metrics agents, and
    # other infrastructure components work on every node type.
    _ADDON_NODE_TOLERATIONS = [
        {"key": "nvidia.com/gpu", "operator": "Exists", "effect": "NoSchedule"},
        {"key": "aws.amazon.com/neuron", "operator": "Exists", "effect": "NoSchedule"},
        {"key": "vpc.amazonaws.com/efa", "operator": "Exists", "effect": "NoSchedule"},
    ]

    def _create_pod_identity_agent_addon(self) -> None:
        """Create EKS Pod Identity Agent add-on.

        On Auto Mode the DaemonSet schedules 0 pods (the agent is built into
        the node runtime), but the add-on registration is still required for
        the control-plane admission webhook that injects Pod Identity tokens.
        """
        eks.Addon(
            self,
            "PodIdentityAgentAddon",
            cluster=self.cluster,  # type: ignore[arg-type]
            addon_name="eks-pod-identity-agent",
            addon_version=EKS_ADDON_POD_IDENTITY_AGENT,
            preserve_on_delete=False,
            configuration_values={
                "tolerations": self._ADDON_NODE_TOLERATIONS,
            },
        )

    def _create_metrics_server_addon(self) -> None:
        """Create Metrics Server add-on for resource metrics.

        The Metrics Server collects resource metrics from kubelets and exposes
        them via the Kubernetes API server. This is required for:
        - Horizontal Pod Autoscaler (HPA)
        - Vertical Pod Autoscaler (VPA)
        - kubectl top commands
        - Resource monitoring dashboards

        Note: Metrics Server doesn't require an IRSA role as it only needs
        in-cluster permissions which are handled by its service account.
        """
        eks.Addon(
            self,
            "MetricsServerAddon",
            cluster=self.cluster,  # type: ignore[arg-type]
            addon_name="metrics-server",
            addon_version=EKS_ADDON_METRICS_SERVER,
            preserve_on_delete=False,
            configuration_values={
                "tolerations": self._ADDON_NODE_TOLERATIONS,
            },
        )

    def _create_efs_csi_driver_addon(self) -> None:
        """Create EFS CSI Driver add-on for shared storage support.

        The EFS CSI driver enables Kubernetes pods to mount EFS file systems
        as persistent volumes. This is required for the shared storage feature.

        We create a Pod Identity role for the EFS CSI driver and update the add-on
        to use it via a custom resource after the add-on is created.
        """
        # Create IAM role for EFS CSI Driver using IRSA + Pod Identity
        self.efs_csi_role = GCORegionalStack._create_irsa_role(
            self,
            "EfsCsiDriverRole",
            oidc_provider_arn=self.oidc_provider.open_id_connect_provider_arn,
            oidc_issuer_url=self.cluster.cluster_open_id_connect_issuer_url,
            service_account_names=["efs-csi-controller-sa"],
            namespaces=["kube-system"],
        )

        # Add EFS CSI driver permissions
        self.efs_csi_role.add_managed_policy(
            iam.ManagedPolicy.from_aws_managed_policy_name("service-role/AmazonEFSCSIDriverPolicy")
        )

        # Create EFS CSI Driver add-on
        efs_addon = eks.Addon(
            self,
            "EfsCsiDriverAddon",
            cluster=self.cluster,  # type: ignore[arg-type]
            addon_name="aws-efs-csi-driver",
            addon_version=EKS_ADDON_EFS_CSI_DRIVER,
            preserve_on_delete=False,
            configuration_values={
                "node": {
                    "tolerations": self._ADDON_NODE_TOLERATIONS,
                },
                "controller": {
                    "tolerations": self._ADDON_NODE_TOLERATIONS,
                },
            },
        )

        # Append the PassRole statement for the EFS CSI role to the shared
        # AwsCustomResource execution role. See the role's creation in
        # _create_aws_custom_resource_role for the full rationale on why
        # we pre-create + attach up-front instead of letting CDK
        # auto-generate per-CR roles.
        self.aws_custom_resource_role.add_to_policy(
            iam.PolicyStatement(
                effect=iam.Effect.ALLOW,
                actions=["iam:PassRole"],
                resources=[self.efs_csi_role.role_arn],
            )
        )

        # Update the add-on to use the IRSA role via custom resource
        # This is needed because the eks v2 alpha Addon doesn't support service_account_role directly
        update_addon = cr.AwsCustomResource(
            self,
            "UpdateEfsCsiAddonRole",
            on_create=cr.AwsSdkCall(
                service="EKS",
                action="updateAddon",
                parameters={
                    "clusterName": self.cluster.cluster_name,
                    "addonName": "aws-efs-csi-driver",
                    "serviceAccountRoleArn": self.efs_csi_role.role_arn,
                },
                physical_resource_id=cr.PhysicalResourceId.of(
                    f"{self.cluster.cluster_name}-efs-csi-role-update"
                ),
            ),
            on_update=cr.AwsSdkCall(
                service="EKS",
                action="updateAddon",
                parameters={
                    "clusterName": self.cluster.cluster_name,
                    "addonName": "aws-efs-csi-driver",
                    "serviceAccountRoleArn": self.efs_csi_role.role_arn,
                },
            ),
            role=self.aws_custom_resource_role,
        )

        # Ensure the update happens after the add-on is created. We also
        # depend on the shared execution role so CloudFormation has fully
        # attached + replicated its inline policy before the Lambda fires.
        update_addon.node.add_dependency(efs_addon)
        update_addon.node.add_dependency(self.efs_csi_role)
        update_addon.node.add_dependency(self.aws_custom_resource_role)

        # Expose the update-addon resource so _apply_kubernetes_manifests can
        # make the kubectl Lambda wait for the IRSA annotation patch to land
        # before it tries to rollout-restart the efs-csi-controller. Without
        # this ordering, the restart could fire before EKS has re-attached
        # the role ARN, leaving the new pods just as credential-less as the
        # old ones and causing every EFS CreateAccessPoint to fail with a
        # 401 from IMDS.
        self._efs_csi_addon_role_update = update_addon

    def _create_cloudwatch_observability_addon(self) -> None:
        """Create CloudWatch Observability add-on for Container Insights.

        The CloudWatch Observability add-on enables Container Insights metrics
        for the EKS cluster, providing visibility into:
        - Cluster CPU and memory utilization
        - Node-level metrics
        - Pod and container metrics
        - Application logs (optional)

        These metrics are used by the monitoring dashboard to display
        cluster health and resource utilization.
        """

        # Create IAM role for CloudWatch agent using IRSA + Pod Identity
        self.cloudwatch_role = GCORegionalStack._create_irsa_role(
            self,
            "CloudWatchObservabilityRole",
            oidc_provider_arn=self.oidc_provider.open_id_connect_provider_arn,
            oidc_issuer_url=self.cluster.cluster_open_id_connect_issuer_url,
            service_account_names=["cloudwatch-agent"],
            namespaces=["amazon-cloudwatch"],
        )

        # Add CloudWatch agent permissions
        self.cloudwatch_role.add_managed_policy(
            iam.ManagedPolicy.from_aws_managed_policy_name("CloudWatchAgentServerPolicy")
        )
        self.cloudwatch_role.add_managed_policy(
            iam.ManagedPolicy.from_aws_managed_policy_name("AWSXrayWriteOnlyAccess")
        )

        # Create CloudWatch Observability add-on
        cw_addon = eks.Addon(
            self,
            "CloudWatchObservabilityAddon",
            cluster=self.cluster,  # type: ignore[arg-type]
            addon_name="amazon-cloudwatch-observability",
            addon_version=EKS_ADDON_CLOUDWATCH_OBSERVABILITY,
            preserve_on_delete=False,
            configuration_values={
                "tolerations": self._ADDON_NODE_TOLERATIONS,
                # Enable Container Insights with application log collection
                # Logs are sent to /aws/containerinsights/{cluster}/application
                "containerLogs": {
                    "enabled": True,
                },
            },
        )

        # Append the PassRole statement for the CloudWatch Observability
        # role to the shared AwsCustomResource execution role. See
        # _create_aws_custom_resource_role for the full rationale.
        self.aws_custom_resource_role.add_to_policy(
            iam.PolicyStatement(
                effect=iam.Effect.ALLOW,
                actions=["iam:PassRole"],
                resources=[self.cloudwatch_role.role_arn],
            )
        )

        # Update the add-on to use the IRSA role via custom resource
        update_cw_addon = cr.AwsCustomResource(
            self,
            "UpdateCloudWatchAddonRole",
            on_create=cr.AwsSdkCall(
                service="EKS",
                action="updateAddon",
                parameters={
                    "clusterName": self.cluster.cluster_name,
                    "addonName": "amazon-cloudwatch-observability",
                    "serviceAccountRoleArn": self.cloudwatch_role.role_arn,
                },
                physical_resource_id=cr.PhysicalResourceId.of(
                    f"{self.cluster.cluster_name}-cw-obs-role-update"
                ),
            ),
            on_update=cr.AwsSdkCall(
                service="EKS",
                action="updateAddon",
                parameters={
                    "clusterName": self.cluster.cluster_name,
                    "addonName": "amazon-cloudwatch-observability",
                    "serviceAccountRoleArn": self.cloudwatch_role.role_arn,
                },
            ),
            role=self.aws_custom_resource_role,
        )

        # Ensure the update happens after the add-on is created. Depend on
        # the shared execution role so CFN has fully attached + replicated
        # its inline policy before the Lambda fires. No CR→CR dependency
        # chain needed anymore — the race it was serializing against is
        # eliminated by pre-creating the role.
        update_cw_addon.node.add_dependency(cw_addon)
        update_cw_addon.node.add_dependency(self.cloudwatch_role)
        update_cw_addon.node.add_dependency(self.aws_custom_resource_role)

        # Expose the update-addon resource so _apply_kubernetes_manifests can
        # make the kubectl Lambda wait for the IRSA annotation patch to land
        # before it rollout-restarts the cloudwatch-agent DaemonSet. See the
        # EFS CSI equivalent for the full rationale — same race, same fix.
        self._cloudwatch_addon_role_update = update_cw_addon

    def _create_service_account_role(self) -> None:
        """Create IAM role for Kubernetes service account using EKS Pod Identity.

        Pod Identity is the recommended mechanism for EKS Auto Mode. It's simpler
        and more reliable than IRSA — no OIDC provider, no webhook injection, no
        projected tokens. EKS manages the credential injection automatically.

        This role can be assumed by the gco-service-account in:
        - gco-system namespace (for system services like health-monitor, manifest-processor)
        - gco-jobs namespace (for user jobs that need SQS access for KEDA scaling)
        - gco-inference namespace (for inference endpoints)
        """
        # Create IAM role with IRSA (OIDC) trust + Pod Identity trust
        #
        # The trust policy's `sub` condition must list every ServiceAccount
        # that needs to assume this role. Keep in sync with:
        #   - lambda/kubectl-applier-simple/manifests/01-serviceaccounts.yaml
        #     (gco-service-account)
        #   - lambda/kubectl-applier-simple/manifests/02-rbac.yaml
        #     (gco-health-monitor-sa, gco-manifest-processor-sa,
        #      gco-inference-monitor-sa)
        #   - lambda/kubectl-applier-simple/manifests/04a-jobs-serviceaccount.yaml
        #     (gco-service-account in gco-jobs)
        self.service_account_role = GCORegionalStack._create_irsa_role(
            self,
            "ServiceAccountRole",
            oidc_provider_arn=self.oidc_provider.open_id_connect_provider_arn,
            oidc_issuer_url=self.cluster.cluster_open_id_connect_issuer_url,
            service_account_names=[
                "gco-service-account",
                "gco-health-monitor-sa",
                "gco-manifest-processor-sa",
                "gco-inference-monitor-sa",
            ],
            namespaces=["gco-system", "gco-jobs", "gco-inference"],
        )

        # Grant permission to read the auth secret
        # Note: We use an explicit IAM policy statement with a wildcard (*) because:
        # 1. The secret is in a different region (API Gateway region)
        # 2. CDK's grant_read() generates a policy with ?????? suffix which requires
        #    exactly 6 characters, but the SDK can call GetSecretValue with either
        #    the full ARN (with suffix) or partial ARN (without suffix)
        # 3. Using * ensures both forms work correctly
        self.service_account_role.add_to_policy(
            iam.PolicyStatement(
                effect=iam.Effect.ALLOW,
                actions=[
                    "secretsmanager:GetSecretValue",
                    "secretsmanager:DescribeSecret",
                ],
                resources=[f"{self.auth_secret_arn}*"],  # Wildcard to match with or without suffix
            )
        )

        # NOTE: the trailing ``*`` on the auth-secret ARN above matches the
        # random 6-character suffix Secrets Manager appends to secret ARNs.
        # cdk-nag v3 does not raise an IAM5 finding on it: the secret is a
        # cross-stack ``Fn::ImportValue`` token, and the IAM5 wildcard check
        # does not flag import-based resources — so no acknowledgment is
        # needed here (verified across the full nag config matrix).
        from gco.stacks.nag_suppressions import acknowledge_nag_findings

        # cdk-nag suppression: the ServiceAccountRole grants ec2:Describe*
        # and elasticloadbalancing:Describe* for the AWS Load Balancer
        # Controller. These AWS APIs do not support resource-level IAM
        # scoping — Resource: * is the only valid form.
        acknowledge_nag_findings(
            self.service_account_role,
            [
                {
                    "id": "AwsSolutions-IAM5",
                    "reason": (
                        "The ServiceAccountRole grants ec2:Describe* and "
                        "elasticloadbalancing:Describe* for the AWS Load Balancer "
                        "Controller. These AWS APIs do not support resource-level "
                        "IAM scoping — Resource: * is the only valid form. See "
                        "https://docs.aws.amazon.com/service-authorization/latest/"
                        "reference/list_amazonec2.html"
                    ),
                    "appliesTo": ["Resource::*"],
                },
            ],
        )

        # Add permissions for AWS Load Balancer Controller
        self.service_account_role.add_to_policy(
            iam.PolicyStatement(
                effect=iam.Effect.ALLOW,
                actions=[
                    "ec2:DescribeAccountAttributes",
                    "ec2:DescribeAddresses",
                    "ec2:DescribeAvailabilityZones",
                    "ec2:DescribeInternetGateways",
                    "ec2:DescribeVpcs",
                    "ec2:DescribeVpcPeeringConnections",
                    "ec2:DescribeSubnets",
                    "ec2:DescribeSecurityGroups",
                    "ec2:DescribeInstances",
                    "ec2:DescribeNetworkInterfaces",
                    "ec2:DescribeTags",
                    "ec2:GetCoipPoolUsage",
                    "ec2:DescribeCoipPools",
                    "elasticloadbalancing:DescribeLoadBalancers",
                    "elasticloadbalancing:DescribeLoadBalancerAttributes",
                    "elasticloadbalancing:DescribeListeners",
                    "elasticloadbalancing:DescribeListenerCertificates",
                    "elasticloadbalancing:DescribeSSLPolicies",
                    "elasticloadbalancing:DescribeRules",
                    "elasticloadbalancing:DescribeTargetGroups",
                    "elasticloadbalancing:DescribeTargetGroupAttributes",
                    "elasticloadbalancing:DescribeTargetHealth",
                    "elasticloadbalancing:DescribeTags",
                ],
                resources=["*"],
            )
        )

        self.service_account_role.add_to_policy(
            iam.PolicyStatement(
                effect=iam.Effect.ALLOW,
                actions=[
                    "elasticloadbalancing:CreateLoadBalancer",
                    "elasticloadbalancing:CreateTargetGroup",
                    "elasticloadbalancing:CreateListener",
                    "elasticloadbalancing:DeleteLoadBalancer",
                    "elasticloadbalancing:DeleteTargetGroup",
                    "elasticloadbalancing:DeleteListener",
                    "elasticloadbalancing:ModifyLoadBalancerAttributes",
                    "elasticloadbalancing:ModifyTargetGroup",
                    "elasticloadbalancing:ModifyTargetGroupAttributes",
                    "elasticloadbalancing:ModifyListener",
                    "elasticloadbalancing:RegisterTargets",
                    "elasticloadbalancing:DeregisterTargets",
                    "elasticloadbalancing:SetWebAcl",
                    "elasticloadbalancing:SetSecurityGroups",
                    "elasticloadbalancing:SetSubnets",
                    "elasticloadbalancing:AddTags",
                    "elasticloadbalancing:RemoveTags",
                ],
                resources=["*"],
            )
        )

        self.service_account_role.add_to_policy(
            iam.PolicyStatement(
                effect=iam.Effect.ALLOW,
                actions=[
                    "ec2:CreateSecurityGroup",
                    "ec2:CreateTags",
                    "ec2:DeleteTags",
                    "ec2:AuthorizeSecurityGroupIngress",
                    "ec2:RevokeSecurityGroupIngress",
                    "ec2:DeleteSecurityGroup",
                ],
                resources=["*"],
            )
        )

        self.service_account_role.add_to_policy(
            iam.PolicyStatement(
                effect=iam.Effect.ALLOW,
                actions=["iam:CreateServiceLinkedRole"],
                resources=["*"],
                conditions={
                    "StringEquals": {"iam:AWSServiceName": "elasticloadbalancing.amazonaws.com"}
                },
            )
        )

        self.service_account_role.add_to_policy(
            iam.PolicyStatement(
                effect=iam.Effect.ALLOW,
                actions=[
                    "wafv2:GetWebACL",
                    "wafv2:GetWebACLForResource",
                    "wafv2:AssociateWebACL",
                    "wafv2:DisassociateWebACL",
                ],
                resources=["*"],
            )
        )

        self.service_account_role.add_to_policy(
            iam.PolicyStatement(
                effect=iam.Effect.ALLOW,
                actions=[
                    "shield:GetSubscriptionState",
                    "shield:DescribeProtection",
                    "shield:CreateProtection",
                    "shield:DeleteProtection",
                ],
                resources=["*"],
            )
        )

        self.service_account_role.add_to_policy(
            iam.PolicyStatement(
                effect=iam.Effect.ALLOW,
                actions=["acm:ListCertificates", "acm:DescribeCertificate"],
                resources=["*"],
            )
        )

        self.service_account_role.add_to_policy(
            iam.PolicyStatement(
                effect=iam.Effect.ALLOW,
                actions=["cognito-idp:DescribeUserPoolClient"],
                resources=["*"],
            )
        )

        # Add SQS permissions for KEDA to scale based on queue depth
        self.service_account_role.add_to_policy(
            iam.PolicyStatement(
                effect=iam.Effect.ALLOW,
                actions=[
                    "sqs:GetQueueAttributes",
                    "sqs:GetQueueUrl",
                    "sqs:ReceiveMessage",
                    "sqs:DeleteMessage",
                    "sqs:SendMessage",
                ],
                resources=[
                    self.job_queue.queue_arn,
                    self.job_dlq.queue_arn,
                ],
            )
        )

        # Add CloudWatch permissions for publishing custom metrics
        # Used by health-monitor and manifest-processor to publish metrics
        self.service_account_role.add_to_policy(
            iam.PolicyStatement(
                effect=iam.Effect.ALLOW,
                actions=["cloudwatch:PutMetricData"],
                resources=["*"],
                conditions={
                    "StringEquals": {
                        "cloudwatch:namespace": [
                            "GCO/HealthMonitor",
                            "GCO/ManifestProcessor",
                        ]
                    }
                },
            )
        )

        # Add DynamoDB permissions for templates, webhooks, and job queue
        # Tables are created in the global stack and accessed from all regions
        project_name = self.config.get_project_name()
        global_region = self.config.get_global_region()

        self.service_account_role.add_to_policy(
            iam.PolicyStatement(
                effect=iam.Effect.ALLOW,
                actions=[
                    "dynamodb:GetItem",
                    "dynamodb:PutItem",
                    "dynamodb:UpdateItem",
                    "dynamodb:DeleteItem",
                    "dynamodb:Query",
                    "dynamodb:Scan",
                ],
                resources=[
                    f"arn:aws:dynamodb:{global_region}:{self.account}:table/{project_name}-job-templates",
                    f"arn:aws:dynamodb:{global_region}:{self.account}:table/{project_name}-job-templates/index/*",
                    f"arn:aws:dynamodb:{global_region}:{self.account}:table/{project_name}-webhooks",
                    f"arn:aws:dynamodb:{global_region}:{self.account}:table/{project_name}-webhooks/index/*",
                    f"arn:aws:dynamodb:{global_region}:{self.account}:table/{project_name}-jobs",
                    f"arn:aws:dynamodb:{global_region}:{self.account}:table/{project_name}-jobs/index/*",
                    f"arn:aws:dynamodb:{global_region}:{self.account}:table/{project_name}-inference-endpoints",
                    f"arn:aws:dynamodb:{global_region}:{self.account}:table/{project_name}-inference-endpoints/index/*",
                ],
            )
        )

        # Add S3 permissions for model weights bucket (used by inference init containers)
        self.service_account_role.add_to_policy(
            iam.PolicyStatement(
                effect=iam.Effect.ALLOW,
                actions=[
                    "s3:GetObject",
                    "s3:ListBucket",
                ],
                resources=[
                    f"arn:aws:s3:::{project_name}-*",
                    f"arn:aws:s3:::{project_name}-*/*",
                ],
            )
        )

        # KMS decrypt for model weights bucket (S3-scoped)
        self.service_account_role.add_to_policy(
            iam.PolicyStatement(
                effect=iam.Effect.ALLOW,
                actions=["kms:Decrypt", "kms:GenerateDataKey"],
                resources=[f"arn:aws:kms:*:{self.account}:key/*"],
                conditions={
                    "StringLike": {
                        "kms:ViaService": "s3.*.amazonaws.com",
                    }
                },
            )
        )

        # Create KEDA operator IAM role for SQS access
        self._create_keda_operator_role()

        # Create Pod Identity Associations for all service accounts
        self._create_pod_identity_associations()

    def _create_keda_operator_role(self) -> None:
        """Create IAM role for KEDA operator service account using EKS Pod Identity.

        This role allows the KEDA operator to access SQS queues for scaling
        based on queue depth. The role is assumed by the keda-operator service
        account in the keda namespace.
        """
        # Create IAM role with IRSA (OIDC) trust + Pod Identity trust
        self.keda_operator_role = GCORegionalStack._create_irsa_role(
            self,
            "KedaOperatorRole",
            oidc_provider_arn=self.oidc_provider.open_id_connect_provider_arn,
            oidc_issuer_url=self.cluster.cluster_open_id_connect_issuer_url,
            service_account_names=["keda-operator"],
            namespaces=["keda"],
        )

        # Add SQS permissions for KEDA to read queue metrics
        self.keda_operator_role.add_to_policy(
            iam.PolicyStatement(
                effect=iam.Effect.ALLOW,
                actions=[
                    "sqs:GetQueueAttributes",
                    "sqs:GetQueueUrl",
                ],
                resources=[
                    self.job_queue.queue_arn,
                    self.job_dlq.queue_arn,
                ],
            )
        )

        # CloudWatch read permissions for GPU-based autoscaling. The KEDA
        # aws-cloudwatch scaler reads ContainerInsights GPU utilization metrics
        # to scale inference roles that request GPUs — GPU is not a native HPA
        # resource metric, so this is the only path that can drive GPU scaling.
        # The CloudWatch read APIs do not support resource-level IAM scoping, so
        # they are granted account-wide (read-only).
        self.keda_operator_role.add_to_policy(
            iam.PolicyStatement(
                effect=iam.Effect.ALLOW,
                actions=[
                    "cloudwatch:GetMetricData",
                    "cloudwatch:GetMetricStatistics",
                    "cloudwatch:ListMetrics",
                ],
                resources=["*"],
            )
        )

        # cdk-nag suppression: the CloudWatch metric-read APIs do not support
        # resource-level IAM scoping — Resource: * is the only valid form.
        from gco.stacks.nag_suppressions import acknowledge_nag_findings

        acknowledge_nag_findings(
            self.keda_operator_role,
            [
                {
                    "id": "AwsSolutions-IAM5",
                    "reason": (
                        "The KEDA operator reads CloudWatch metrics "
                        "(GetMetricData, GetMetricStatistics, ListMetrics) to "
                        "drive GPU-based autoscaling. These APIs do not support "
                        "resource-level IAM scoping — Resource: * is the only "
                        "valid form. The grant is read-only."
                    ),
                    "appliesTo": ["Resource::*"],
                },
            ],
        )

    def _create_pod_identity_associations(self) -> None:
        """Create EKS Pod Identity Associations for all service accounts.

        Pod Identity is the recommended mechanism for EKS Auto Mode. Each
        association links an IAM role to a Kubernetes service account in a
        specific namespace. EKS manages credential injection automatically.

        Stores associations in self._pod_identity_associations so the
        kubectl-applier custom resource can declare an explicit dependency,
        ensuring credentials are available before workloads start.
        """
        self._pod_identity_associations: list[Any] = []

        # GCO service account — used by health-monitor, manifest-processor, inference-monitor
        for namespace in ["gco-system", "gco-jobs", "gco-inference"]:
            assoc = eks_l1.CfnPodIdentityAssociation(
                self,
                f"PodIdentity-gco-sa-{namespace}",
                cluster_name=self.cluster.cluster_name,
                namespace=namespace,
                service_account="gco-service-account",
                role_arn=self.service_account_role.role_arn,
            )
            self._pod_identity_associations.append(assoc)

        # KEDA operator — needs SQS access for queue-based scaling
        keda_assoc = eks_l1.CfnPodIdentityAssociation(
            self,
            "PodIdentity-keda-operator",
            cluster_name=self.cluster.cluster_name,
            namespace="keda",
            service_account="keda-operator",
            role_arn=self.keda_operator_role.role_arn,
        )
        self._pod_identity_associations.append(keda_assoc)

        # EFS CSI driver — needs EFS access for shared storage
        efs_assoc = eks_l1.CfnPodIdentityAssociation(
            self,
            "PodIdentity-efs-csi",
            cluster_name=self.cluster.cluster_name,
            namespace="kube-system",
            service_account="efs-csi-controller-sa",
            role_arn=self.efs_csi_role.role_arn,
        )
        self._pod_identity_associations.append(efs_assoc)

        # CloudWatch agent — needs CloudWatch access for observability
        cw_assoc = eks_l1.CfnPodIdentityAssociation(
            self,
            "PodIdentity-cloudwatch",
            cluster_name=self.cluster.cluster_name,
            namespace="amazon-cloudwatch",
            service_account="cloudwatch-agent",
            role_arn=self.cloudwatch_role.role_arn,
        )
        self._pod_identity_associations.append(cw_assoc)

        # FSx CSI driver — only when FSx is enabled (created later in _create_fsx_lustre)
        # The FSx Pod Identity association is added in _create_fsx_lustre instead

    def _resolve_cluster_shared_bucket_from_ssm(self) -> SharedBucketIdentity:
        """Resolve the ``Cluster_Shared_Bucket`` identity from cross-region SSM.

        ``GCOGlobalStack`` publishes three ``ssm.StringParameter``s in the
        global region at ``/gco/cluster-shared-bucket/{name,arn,region}``.
        This method reads them back from the regional stack via
        ``cr.AwsCustomResource`` with ``service="SSM"``,
        ``action="getParameter"``, and ``region=<global-region>`` — matching
        the cross-region read pattern already used in
        ``_create_ga_registration_lambda`` for the Global Accelerator
        endpoint group ARN.

        Runs unconditionally in ``__init__`` — no feature toggle, no
        conditional guard. The returned :class:`SharedBucketIdentity` feeds
        ``_grant_cluster_shared_bucket_to_job_role`` (IAM) and the
        ``image_replacements`` dict (ConfigMap) downstream.

        Returns:
            :class:`SharedBucketIdentity` with ``name``, ``arn``, and
            ``region`` populated as CDK tokens that resolve at deploy time.
        """
        from gco.stacks.nag_suppressions import acknowledge_nag_findings

        global_region = self.config.get_global_region()
        resolved: dict[str, str] = {}

        for suffix in ("name", "arn", "region"):
            parameter_name = f"{CLUSTER_SHARED_SSM_PARAMETER_PREFIX}/{suffix}"
            read_cr = cr.AwsCustomResource(
                self,
                f"ReadClusterSharedBucket{suffix.capitalize()}",
                on_create=cr.AwsSdkCall(
                    service="SSM",
                    action="getParameter",
                    parameters={"Name": parameter_name},
                    region=global_region,
                    physical_resource_id=cr.PhysicalResourceId.of(f"cluster-shared-{suffix}"),
                ),
                on_update=cr.AwsSdkCall(
                    service="SSM",
                    action="getParameter",
                    parameters={"Name": parameter_name},
                    region=global_region,
                    physical_resource_id=cr.PhysicalResourceId.of(f"cluster-shared-{suffix}"),
                ),
                # Cross-region SSM GetParameter doesn't support resource-level
                # scoping cleanly — the principal evaluating the call lives in
                # this stack's region but the parameter lives in the global
                # region. ANY_RESOURCE is the AWS-documented escape hatch; the
                # resulting AwsSolutions-IAM5 nag finding is suppressed in a
                # scoped add_resource_suppressions call below.
                policy=cr.AwsCustomResourcePolicy.from_sdk_calls(
                    resources=cr.AwsCustomResourcePolicy.ANY_RESOURCE
                ),
            )

            # Scoped suppression: the CR policy is Resource::* because the
            # SSM parameter lives in the global region (cross-region calls
            # don't support resource-level scoping cleanly). The action is
            # fixed to ssm:GetParameter and the parameter Name is fixed to
            # a literal string, so the wildcard can only ever read one
            # parameter.
            acknowledge_nag_findings(
                read_cr,
                [
                    {
                        "id": "AwsSolutions-IAM5",
                        "reason": (
                            "Cross-region ssm:GetParameter for "
                            f"{parameter_name} in the global region. The "
                            "AwsCustomResource SDK-call policy is scoped to "
                            "a single fixed action (ssm:GetParameter) with "
                            "a fixed parameter Name — the Resource: * is "
                            "the CDK-documented escape hatch because the "
                            "parameter ARN is not known to the calling "
                            "principal's region. Effective blast radius: "
                            "one parameter."
                        ),
                        "appliesTo": ["Resource::*"],
                    },
                ],
            )

            resolved[suffix] = read_cr.get_response_field("Parameter.Value")

        return SharedBucketIdentity(
            name=resolved["name"],
            arn=resolved["arn"],
            region=resolved["region"],
        )

    def _grant_cluster_shared_bucket_to_job_role(self, shared: SharedBucketIdentity) -> None:
        """Attach RW + KMS permissions on ``Cluster_Shared_Bucket`` to the job-pod role.

        Two ``iam.PolicyStatement``s are added to ``self.service_account_role``
        (the EKS Pod Identity role used by every pod in ``gco-jobs``,
        ``gco-system``, and ``gco-inference``):

        1. S3 object + bucket-level actions (``GetObject``, ``PutObject``,
           ``DeleteObject``, ``ListBucket``, ``GetBucketLocation``) scoped
           to ``<shared.arn>`` and ``<shared.arn>/*`` — the bucket-ARN
           shape uses the ``gco-cluster-shared-*`` prefix that IAM
           policies scope against.
        2. KMS ``Decrypt`` / ``GenerateDataKey`` scoped by the
           ``kms:ViaService=s3.<shared.region>.amazonaws.com`` condition —
           ``resources=["*"]`` because the KMS key ARN is not known to this
           stack (it lives in the global region and is referenced indirectly
           through the S3 service). The condition is what actually restricts
           the grant to the cluster-shared bucket's key.

        Runs unconditionally — the grant is
        present on every regional cluster whether or not analytics is
        enabled.
        """
        from gco.stacks.nag_suppressions import acknowledge_nag_findings

        self.service_account_role.add_to_policy(
            iam.PolicyStatement(
                effect=iam.Effect.ALLOW,
                actions=[
                    "s3:GetObject",
                    "s3:PutObject",
                    "s3:DeleteObject",
                    "s3:ListBucket",
                    "s3:GetBucketLocation",
                ],
                resources=[shared.arn, f"{shared.arn}/*"],
            )
        )

        self.service_account_role.add_to_policy(
            iam.PolicyStatement(
                effect=iam.Effect.ALLOW,
                actions=["kms:Decrypt", "kms:GenerateDataKey"],
                resources=["*"],
                conditions={
                    "StringEquals": {
                        "kms:ViaService": f"s3.{shared.region}.amazonaws.com",
                    }
                },
            )
        )

        # The S3 bucket-ARN resource uses a ``<arn>/*`` object-key wildcard
        # which cdk-nag flags as a wildcard resource. The ARN itself is the
        # literal Cluster_Shared_Bucket ARN resolved from SSM — the ``/*``
        # covers all object keys inside that single bucket, which is the
        # intended semantic for the RW grant.
        acknowledge_nag_findings(
            self.service_account_role,
            [
                {
                    "id": "AwsSolutions-IAM5",
                    "reason": (
                        "The Cluster_Shared_Bucket RW grant uses an <arn>/* "
                        "object-key wildcard on the literal cluster-shared "
                        "bucket ARN resolved from SSM. The wildcard covers "
                        "object keys within a single bucket (the always-on "
                        "gco-cluster-shared-<account>-<region> bucket) — "
                        "this is the standard shape for a bucket-scoped "
                        "RW grant and is what the allow-list assertion "
                        "is written against."
                    ),
                    "appliesTo": [
                        "Resource::<ReadClusterSharedBucketArn4B0BD291.Parameter.Value>/*",
                    ],
                },
            ],
        )

    def _create_regional_shared_bucket(self) -> None:
        """Create the always-on general-purpose regional bucket for this region.

        Provisioned unconditionally — there is no ``cdk.json`` toggle and no
        feature flag that can suppress it — in addition to the central buckets
        owned by ``GCOGlobalStack`` (the model bucket and the cluster-shared
        bucket). The bucket is general purpose: any in-region workload may use
        it, and the per-region cold KV tier auto-targets it when cold-tier
        storage is requested. Its existence is independent of any endpoint's
        cold-tier choice.

        Three constructs are created, mirroring the cluster-shared bucket
        pattern in ``GCOGlobalStack``:

        1. ``regional_shared_kms_key`` — a customer-managed KMS key with annual
           rotation and a 7-day pending window on destroy. The key policy grants
           the ``s3.amazonaws.com`` and ``logs.<region>.amazonaws.com`` service
           principals encrypt/decrypt so S3 server-side encryption and access-log
           delivery work without role-side grants.
        2. ``regional_shared_access_logs_bucket`` — the dedicated S3 access-logs
           destination for the primary bucket.
        3. ``regional_shared_bucket`` — the primary bucket named
           ``gco-regional-shared-<account>-<region>`` (the prefix
           ``REGIONAL_SHARED_BUCKET_NAME_PREFIX`` is the stable ARN prefix used
           by IAM policies and nag assertions). KMS-encrypted with
           ``regional_shared_kms_key``, block-public-access on, SSL enforced,
           versioned, destroy-on-teardown.

        An explicit ``Deny`` for ``aws:SecureTransport=false`` is added to the
        bucket policy independent of ``enforce_ssl=True`` so the deny is
        verifiable in the synthesized template under a known SID.
        """
        # KMS key for the regional bucket. Matches the cluster-shared key
        # posture: annual rotation, 7-day pending window, destroy-on-teardown.
        self.regional_shared_kms_key = kms.Key(
            self,
            "RegionalSharedKmsKey",
            description=(
                "Customer-managed KMS key for the always-on general-purpose "
                "regional bucket in this region's GCORegionalStack."
            ),
            enable_key_rotation=True,
            pending_window=Duration.days(7),
            removal_policy=RemovalPolicy.DESTROY,
        )

        # Key-policy grants for the service principals that encrypt/decrypt on
        # behalf of the bucket (S3 server-side encryption) and the access-logs
        # bucket (CloudWatch/S3 log delivery).
        kms_actions = [
            "kms:Encrypt",
            "kms:Decrypt",
            "kms:ReEncrypt*",
            "kms:GenerateDataKey*",
            "kms:DescribeKey",
        ]

        self.regional_shared_kms_key.add_to_resource_policy(
            iam.PolicyStatement(
                sid="AllowS3ServiceEncryptDecrypt",
                effect=iam.Effect.ALLOW,
                principals=[iam.ServicePrincipal("s3.amazonaws.com")],
                actions=kms_actions,
                resources=["*"],
            )
        )

        self.regional_shared_kms_key.add_to_resource_policy(
            iam.PolicyStatement(
                sid="AllowCloudWatchLogsEncryptDecrypt",
                effect=iam.Effect.ALLOW,
                principals=[iam.ServicePrincipal(f"logs.{self.deployment_region}.amazonaws.com")],
                actions=kms_actions,
                resources=["*"],
            )
        )

        # Retention for the access-logs bucket honors the same `s3_access_logs`
        # context field used by the central buckets (default 90 days).
        s3_access_logs_ctx = self.node.try_get_context("s3_access_logs") or {}
        access_logs_retention_days = int(s3_access_logs_ctx.get("retention_days", 90))

        # Dedicated access-logs bucket for the regional bucket, encrypted with
        # the regional KMS key (its key policy grants the logs service principal
        # encrypt/decrypt).
        self.regional_shared_access_logs_bucket = s3.Bucket(
            self,
            "RegionalSharedAccessLogsBucket",
            encryption=s3.BucketEncryption.KMS,
            encryption_key=self.regional_shared_kms_key,
            block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
            enforce_ssl=True,
            versioned=True,
            removal_policy=RemovalPolicy.DESTROY,
            auto_delete_objects=True,
            lifecycle_rules=[
                s3.LifecycleRule(
                    id="ExpireAccessLogs",
                    enabled=True,
                    expiration=Duration.days(access_logs_retention_days),
                )
            ],
        )

        # Primary general-purpose regional bucket. The name uses the constant
        # prefix so IAM allow-list assertions (arn:aws:s3:::gco-regional-shared-*)
        # stay stable across refactors. `bucket_key_enabled=True` mirrors the
        # central-bucket pattern to reduce per-object KMS request costs.
        self.regional_shared_bucket = s3.Bucket(
            self,
            "RegionalSharedBucket",
            bucket_name=(
                f"{REGIONAL_SHARED_BUCKET_NAME_PREFIX}-{self.account}-{self.deployment_region}"
            ),
            encryption=s3.BucketEncryption.KMS,
            encryption_key=self.regional_shared_kms_key,
            bucket_key_enabled=True,
            block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
            enforce_ssl=True,
            versioned=True,
            removal_policy=RemovalPolicy.DESTROY,
            auto_delete_objects=True,
            server_access_logs_bucket=self.regional_shared_access_logs_bucket,
            server_access_logs_prefix="regional-shared/",
        )

        # Explicit Deny for insecure transport. `enforce_ssl=True` already adds
        # an equivalent statement, but duplicating it here makes the deny
        # verifiable in the synthesized template under a known SID.
        self.regional_shared_bucket.add_to_resource_policy(
            iam.PolicyStatement(
                sid="DenyInsecureTransport",
                effect=iam.Effect.DENY,
                principals=[iam.AnyPrincipal()],
                actions=["s3:*"],
                resources=[
                    self.regional_shared_bucket.bucket_arn,
                    f"{self.regional_shared_bucket.bucket_arn}/*",
                ],
                conditions={"Bool": {"aws:SecureTransport": "false"}},
            )
        )

        # Publish the bucket's identity as three SSM parameters in this
        # region's own parameter store, mirroring how the model bucket and
        # cluster-shared bucket publish theirs. In-region workloads and the
        # regional upload surface resolve the always-on regional bucket by
        # reading these back rather than reconstructing the name. Because the
        # bucket is unconditional, these parameters are always present once the
        # region's stack is deployed. The prefix
        # ``REGIONAL_SHARED_SSM_PARAMETER_PREFIX`` is the single source of truth
        # for the namespace.
        ssm.StringParameter(
            self,
            "RegionalSharedBucketNameParam",
            parameter_name=f"{REGIONAL_SHARED_SSM_PARAMETER_PREFIX}/name",
            string_value=self.regional_shared_bucket.bucket_name,
            description="Name of the always-on general-purpose regional bucket for this region.",
        )

        ssm.StringParameter(
            self,
            "RegionalSharedBucketArnParam",
            parameter_name=f"{REGIONAL_SHARED_SSM_PARAMETER_PREFIX}/arn",
            string_value=self.regional_shared_bucket.bucket_arn,
            description="ARN of the always-on general-purpose regional bucket for this region.",
        )

        ssm.StringParameter(
            self,
            "RegionalSharedBucketRegionParam",
            parameter_name=f"{REGIONAL_SHARED_SSM_PARAMETER_PREFIX}/region",
            string_value=self.deployment_region,
            description="Home region of the always-on general-purpose regional bucket.",
        )

        # CDK-nag suppressions scoped per-resource at the construct site,
        # mirroring the central bucket pattern. Every suppression carries an
        # explicit reason; no blanket bypasses.
        from gco.stacks.nag_suppressions import acknowledge_nag_findings

        regional_replication_reason = (
            "The general-purpose regional bucket is a region-local store; "
            "in-region workloads publish to their own region's bucket and there "
            "is no durability requirement that warrants cross-region "
            "replication. Access logs do not require replication for the same "
            "reason."
        )

        acknowledge_nag_findings(
            self.regional_shared_bucket,
            [
                {
                    "id": "HIPAA.Security-S3BucketReplicationEnabled",
                    "reason": regional_replication_reason,
                },
                {
                    "id": "NIST.800.53.R5-S3BucketReplicationEnabled",
                    "reason": regional_replication_reason,
                },
                {
                    "id": "PCI.DSS.321-S3BucketReplicationEnabled",
                    "reason": regional_replication_reason,
                },
            ],
        )

        access_logs_is_self_target_reason = (
            "This is the server access logs destination bucket for the "
            "general-purpose regional bucket."
        )
        acknowledge_nag_findings(
            self.regional_shared_access_logs_bucket,
            [
                {
                    "id": "AwsSolutions-S1",
                    "reason": access_logs_is_self_target_reason,
                },
                {
                    "id": "HIPAA.Security-S3BucketLoggingEnabled",
                    "reason": access_logs_is_self_target_reason,
                },
                {
                    "id": "NIST.800.53.R5-S3BucketLoggingEnabled",
                    "reason": access_logs_is_self_target_reason,
                },
                {
                    "id": "PCI.DSS.321-S3BucketLoggingEnabled",
                    "reason": access_logs_is_self_target_reason,
                },
                {
                    "id": "HIPAA.Security-S3BucketReplicationEnabled",
                    "reason": regional_replication_reason,
                },
                {
                    "id": "NIST.800.53.R5-S3BucketReplicationEnabled",
                    "reason": regional_replication_reason,
                },
                {
                    "id": "PCI.DSS.321-S3BucketReplicationEnabled",
                    "reason": regional_replication_reason,
                },
            ],
        )

        # Grant the in-region pod role read/write on this bucket and use of its
        # KMS key — and nothing else. The grant lives next to the bucket it
        # scopes to, so the role's regional-bucket access stays exactly as wide
        # as this one bucket and its key.
        self._grant_regional_shared_bucket_to_service_account()

    def _grant_regional_shared_bucket_to_service_account(self) -> None:
        """Attach RW + KMS permissions on the regional bucket to the pod role.

        Two ``iam.PolicyStatement``s are added to ``self.service_account_role``
        (the EKS Pod Identity role used by every pod in ``gco-jobs``,
        ``gco-system``, and ``gco-inference``):

        1. S3 object + bucket-level actions (``GetObject``, ``PutObject``,
           ``DeleteObject``, ``ListBucket``, ``GetBucketLocation``) scoped to
           the literal ``regional_shared_bucket`` ARN and its ``<arn>/*``
           object-key space — and to no other bucket.
        2. KMS ``Decrypt`` / ``Encrypt`` / ``GenerateDataKey`` /
           ``DescribeKey`` scoped to the literal ``regional_shared_kms_key``
           ARN — and to no other key.

        Because both resources are local constructs in this stack, each ARN is
        a concrete reference rather than a wildcard, so the role gains access to
        precisely this bucket and this key. The grant runs unconditionally as
        part of provisioning the always-on regional bucket.
        """
        from gco.stacks.nag_suppressions import acknowledge_nag_findings

        self.service_account_role.add_to_policy(
            iam.PolicyStatement(
                effect=iam.Effect.ALLOW,
                actions=[
                    "s3:GetObject",
                    "s3:PutObject",
                    "s3:DeleteObject",
                    "s3:ListBucket",
                    "s3:GetBucketLocation",
                ],
                resources=[
                    self.regional_shared_bucket.bucket_arn,
                    f"{self.regional_shared_bucket.bucket_arn}/*",
                ],
            )
        )

        self.service_account_role.add_to_policy(
            iam.PolicyStatement(
                effect=iam.Effect.ALLOW,
                actions=[
                    "kms:Decrypt",
                    "kms:Encrypt",
                    "kms:GenerateDataKey",
                    "kms:DescribeKey",
                ],
                resources=[self.regional_shared_kms_key.key_arn],
            )
        )

        # The S3 bucket-ARN resource uses a ``<arn>/*`` object-key wildcard
        # which cdk-nag flags as a wildcard resource. The ARN itself is the
        # literal regional bucket ARN created in this stack — the ``/*`` covers
        # all object keys inside that single bucket, which is the intended
        # semantic for the RW grant. The KMS statement carries no wildcard.
        acknowledge_nag_findings(
            self.service_account_role,
            [
                {
                    "id": "AwsSolutions-IAM5",
                    "reason": (
                        "The regional bucket RW grant uses an <arn>/* "
                        "object-key wildcard on the literal "
                        "gco-regional-shared-<account>-<region> bucket ARN "
                        "created in this stack. The wildcard covers object "
                        "keys within a single bucket — this is the standard "
                        "shape for a bucket-scoped RW grant and is what the "
                        "allow-list assertion is written against."
                    ),
                    "appliesTo": [
                        "Resource::<RegionalSharedBucket3FF19783.Arn>/*",
                    ],
                },
            ],
        )

    def _create_kubectl_lambda(self) -> None:
        """Create Lambda function to apply Kubernetes manifests using Python client.

        Note: This creates the Lambda and provider but does NOT create the custom resource.
        The custom resource is created in _apply_kubernetes_manifests() after ALB is created,
        so that target group ARNs can be passed to the manifests.
        """
        project_name = self.config.get_project_name()

        # Create IAM role for kubectl Lambda
        kubectl_lambda_role = iam.Role(
            self,
            "KubectlLambdaRole",
            assumed_by=iam.ServicePrincipal("lambda.amazonaws.com"),
            managed_policies=[
                iam.ManagedPolicy.from_aws_managed_policy_name(
                    "service-role/AWSLambdaVPCAccessExecutionRole"
                ),
                iam.ManagedPolicy.from_aws_managed_policy_name(
                    "service-role/AWSLambdaBasicExecutionRole"
                ),
            ],
        )

        # Add EKS permissions
        kubectl_lambda_role.add_to_policy(
            iam.PolicyStatement(
                actions=[
                    "eks:DescribeCluster",
                    "eks:ListClusters",
                ],
                resources=[self.cluster.cluster_arn],
            )
        )

        # Add permissions to assume cluster admin role
        kubectl_lambda_role.add_to_policy(
            iam.PolicyStatement(actions=["sts:AssumeRole"], resources=["*"])
        )

        # Allow the convergence apply tasks to record per-phase status to SSM
        # (base-manifests / post-helm-manifests), mirroring the helm worker, so
        # `gco stacks addons status` surfaces the apply passes alongside charts.
        kubectl_lambda_role.add_to_policy(
            iam.PolicyStatement(
                actions=["ssm:PutParameter"],
                resources=[
                    f"arn:aws:ssm:{self.deployment_region}:{self.account}:"
                    f"parameter/{project_name}/addons/*"
                ],
            )
        )

        # Create security group for kubectl Lambda
        kubectl_lambda_sg = ec2.SecurityGroup(
            self,
            "KubectlLambdaSG",
            vpc=self.vpc,
            description="Security group for kubectl Lambda to access EKS cluster",
            security_group_name=f"{self.config.get_project_name()}-kubectl-lambda-sg-{self.deployment_region}",
            allow_all_outbound=True,  # Lambda needs outbound access to EKS API
        )

        # Allow Lambda security group to access EKS cluster security group on port 443
        # The EKS cluster security group is automatically created by EKS
        self.cluster.cluster_security_group.add_ingress_rule(
            peer=kubectl_lambda_sg,
            connection=ec2.Port.tcp(443),
            description="Allow kubectl Lambda to access EKS API",
        )

        # Create Lambda function (Python-only, no Docker!)
        # Store function name as string attribute for cross-stack references
        # This avoids CDK cross-environment resolution issues when account is unresolved
        self.kubectl_lambda_function_name = f"{project_name}-kubectl-{self.deployment_region}"
        self.kubectl_lambda = lambda_.Function(
            self,
            "KubectlApplierFunction",
            function_name=self.kubectl_lambda_function_name,
            runtime=getattr(lambda_.Runtime, LAMBDA_PYTHON_RUNTIME),
            handler="handler.lambda_handler",
            code=lambda_.Code.from_asset("lambda/kubectl-applier-simple-build"),
            timeout=Duration.minutes(15),  # Max Lambda timeout
            memory_size=512,
            role=kubectl_lambda_role,
            vpc=self.vpc,
            vpc_subnets=ec2.SubnetSelection(subnet_type=ec2.SubnetType.PRIVATE_WITH_EGRESS),
            security_groups=[kubectl_lambda_sg],  # Use the security group we created
            environment={
                "CLUSTER_NAME": self.cluster.cluster_name,
                "REGION": self.deployment_region,
                # Lets the convergence apply tasks record per-phase status to
                # SSM (/<project>/addons/<region>/{base,post-helm}-manifests).
                "PROJECT_NAME": project_name,
            },
            tracing=lambda_.Tracing.ACTIVE,
        )

        # Add EKS access entry for the Lambda role to authenticate with the cluster
        # This grants the Lambda role cluster admin permissions
        eks.AccessEntry(
            self,
            "KubectlLambdaAccessEntry",
            cluster=self.cluster,  # type: ignore[arg-type]
            principal=kubectl_lambda_role.role_arn,
            access_policies=[
                eks.AccessPolicy.from_access_policy_name(
                    "AmazonEKSClusterAdminPolicy", access_scope_type=eks.AccessScopeType.CLUSTER
                )
            ],
        )

        # No custom-resource provider needed: the kubectl-applier Lambda is now
        # invoked directly by the convergence state machine (the base and
        # post-Helm apply tasks), not through a CloudFormation custom resource.

        # cdk-nag suppression: the kubectl-applier Lambda requires broad
        # EKS and Kubernetes API access to apply arbitrary manifests.
        from gco.stacks.nag_suppressions import acknowledge_nag_findings

        acknowledge_nag_findings(
            kubectl_lambda_role,
            [
                {
                    "id": "AwsSolutions-IAM5",
                    "reason": (
                        "The kubectl-applier Lambda requires broad EKS and Kubernetes API "
                        "access to apply arbitrary manifests (RBAC, ServiceAccounts, "
                        "Deployments, Jobs, NetworkPolicies) across multiple namespaces. "
                        "Resource: * is required because the set of Kubernetes resources "
                        "is dynamic and not known at synth time."
                    ),
                    "appliesTo": ["Resource::*"],
                },
            ],
        )

    def _apply_kubernetes_manifests(self) -> None:
        """Apply Kubernetes manifests using the kubectl Lambda custom resource.

        This is called after ALB security group and EFS are created.
        The Ingress will use the security group ID to create the ALB.
        """

        # Get public subnet IDs for Ingress annotation (currently unused but kept for future use)
        # public_subnet_ids = ",".join([subnet.subnet_id for subnet in self.vpc.public_subnets])

        # Apply manifests using custom resource
        # Build image replacements dict
        # Include a deployment timestamp to force pod rollouts when code changes
        from datetime import UTC, datetime

        deployment_timestamp = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")

        # Get resource thresholds from config
        thresholds = self.config.get_resource_thresholds()

        # Get manifest processor resource quotas.
        # Resource quotas and the security/image policy now live under the
        # shared job_validation_policy section because both the REST
        # manifest_processor and the SQS queue_processor read them. Service-
        # specific knobs (replicas, validation_enabled, max_request_body_bytes,
        # etc.) stay under manifest_processor.
        mp_config = self.node.try_get_context("manifest_processor") or {}
        job_policy = self.node.try_get_context("job_validation_policy") or {}
        job_quotas = job_policy.get("resource_quotas", {})

        image_replacements = {
            "{{HEALTH_MONITOR_IMAGE}}": self.health_monitor_image.image_uri,
            "{{MANIFEST_PROCESSOR_IMAGE}}": self.manifest_processor_image.image_uri,
            "{{INFERENCE_MONITOR_IMAGE}}": self.inference_monitor_image.image_uri,
            # External, pinned upstream image for the shared Mooncake master
            # (bundles the mooncake_master binary). Same default as disaggregated
            # role pods; per-endpoint spec.mooncake.store.master_image overrides.
            "{{MOONCAKE_MASTER_IMAGE}}": MOONCAKE_MASTER_DEFAULT_IMAGE,
            "{{CLUSTER_NAME}}": self.cluster.cluster_name,
            "{{REGION}}": self.deployment_region,
            "{{AUTH_SECRET_ARN}}": self.auth_secret_arn,
            "{{SERVICE_ACCOUNT_ROLE_ARN}}": self.service_account_role.role_arn,
            "{{EFS_FILE_SYSTEM_ID}}": self.efs_file_system.file_system_id,
            "{{EFS_ACCESS_POINT_ID}}": self.efs_access_point.access_point_id,
            "{{JOB_QUEUE_URL}}": self.job_queue.queue_url,
            "{{JOB_QUEUE_ARN}}": self.job_queue.queue_arn,
            "{{DEPLOYMENT_TIMESTAMP}}": deployment_timestamp,
            # Resource thresholds
            "{{CPU_THRESHOLD}}": str(thresholds.cpu_threshold),
            "{{MEMORY_THRESHOLD}}": str(thresholds.memory_threshold),
            "{{GPU_THRESHOLD}}": str(thresholds.gpu_threshold),
            "{{PENDING_PODS_THRESHOLD}}": str(thresholds.pending_pods_threshold),
            "{{PENDING_REQUESTED_CPU_VCPUS}}": str(thresholds.pending_requested_cpu_vcpus),
            "{{PENDING_REQUESTED_MEMORY_GB}}": str(thresholds.pending_requested_memory_gb),
            "{{PENDING_REQUESTED_GPUS}}": str(thresholds.pending_requested_gpus),
            # DynamoDB table names (from global stack)
            "{{TEMPLATES_TABLE_NAME}}": f"{self.config.get_project_name()}-job-templates",
            "{{WEBHOOKS_TABLE_NAME}}": f"{self.config.get_project_name()}-webhooks",
            "{{JOBS_TABLE_NAME}}": f"{self.config.get_project_name()}-jobs",
            # DynamoDB region (global stack region, may differ from cluster region)
            "{{DYNAMODB_REGION}}": self.config.get_global_region(),
            # Manifest processor resource quotas (sourced from shared policy).
            "{{MP_MAX_CPU_PER_MANIFEST}}": str(job_quotas.get("max_cpu_per_manifest", "10")),
            "{{MP_MAX_MEMORY_PER_MANIFEST}}": str(
                job_quotas.get("max_memory_per_manifest", "32Gi")
            ),
            "{{MP_MAX_GPU_PER_MANIFEST}}": str(job_quotas.get("max_gpu_per_manifest", 4)),
            # Manifest processor namespace allowlist (sourced from shared policy).
            # Both the REST manifest processor and the SQS queue processor
            # read from job_validation_policy.allowed_namespaces so a single
            # edit takes effect on both submission paths at the next deploy.
            "{{MP_ALLOWED_NAMESPACES}}": ",".join(
                job_policy.get("allowed_namespaces", ["default", "gco-jobs"])
            ),
            # Manifest processor image registry allowlist (sourced from shared
            # policy). Augmented with the project's own ECR registry hostnames
            # so jobs built via ``gco images build`` aren't rejected by the
            # REST submission path. Identical augmentation runs on the SQS
            # path below — see ``{{QP_TRUSTED_REGISTRIES}}``.
            "{{MP_TRUSTED_REGISTRIES}}": ",".join(
                _augment_trusted_registries_with_project_ecr(
                    job_policy.get("trusted_registries", []),
                    account=self.account,
                    regions=self.config.get_regions(),
                    global_region=self.config.get_global_region(),
                )
            ),
            "{{MP_TRUSTED_DOCKERHUB_ORGS}}": ",".join(job_policy.get("trusted_dockerhub_orgs", [])),
            # Manifest processor request body size cap (HTTP 413 middleware).
            # Lives at cdk.json::manifest_processor.max_request_body_bytes.
            "{{MP_MAX_REQUEST_BODY_BYTES}}": str(
                mp_config.get("max_request_body_bytes", 1_048_576)
            ),
        }

        # Always-on Cluster_Shared_Bucket replacements. Populated from the
        # SharedBucketIdentity resolved in __init__ via cross-region SSM
        # read from GCOGlobalStack. Never gated on the analytics toggle —
        # the gco-cluster-shared-bucket ConfigMap is applied to every
        # regional cluster.
        image_replacements.update(
            _compute_kubectl_cluster_shared_replacements(self.cluster_shared_identity)
        )

        # Add queue processor replacements if enabled
        qp_config = self.node.try_get_context("queue_processor") or {}

        # Add VPC endpoint CIDR replacements for network policy restrictions
        # Generates a YAML block of ipBlock entries from the vpc_endpoint_cidrs array.
        # The placeholder {{VPC_ENDPOINT_CIDR_BLOCKS}} sits at 8-space indentation in
        # the manifest, so the first entry needs no leading indent (the manifest provides
        # it) and subsequent entries are indented to align.
        vpc_endpoint_cidrs = self.node.try_get_context("vpc_endpoint_cidrs") or ["10.0.0.0/16"]
        cidr_lines = []
        for i, cidr in enumerate(vpc_endpoint_cidrs):
            prefix = "" if i == 0 else "        "
            cidr_lines.append(f'{prefix}- ipBlock:\n            cidr: "{cidr}"')
        image_replacements["{{VPC_ENDPOINT_CIDR_BLOCKS}}"] = "\n".join(cidr_lines)

        # Resource governance for gco-jobs namespace: ResourceQuota caps aggregate
        # resource consumption across the namespace, LimitRange caps per-container
        # maxima. Values come from cdk.json `resource_quota` context with defaults
        # sized for a modest multi-tenant dev cluster.
        resource_quota = self.node.try_get_context("resource_quota") or {}
        image_replacements["{{QUOTA_MAX_CPU}}"] = str(resource_quota.get("max_cpu", "100"))
        image_replacements["{{QUOTA_MAX_MEMORY}}"] = str(resource_quota.get("max_memory", "512Gi"))
        image_replacements["{{QUOTA_MAX_GPU}}"] = str(resource_quota.get("max_gpu", "32"))
        image_replacements["{{QUOTA_MAX_PODS}}"] = str(resource_quota.get("max_pods", "50"))
        image_replacements["{{LIMIT_MAX_CPU}}"] = str(resource_quota.get("container_max_cpu", "10"))
        image_replacements["{{LIMIT_MAX_MEMORY}}"] = str(
            resource_quota.get("container_max_memory", "64Gi")
        )
        image_replacements["{{LIMIT_MAX_GPU}}"] = str(resource_quota.get("container_max_gpu", "4"))

        if self.queue_processor_enabled:
            image_replacements["{{QUEUE_PROCESSOR_IMAGE}}"] = self.queue_processor_image.image_uri
            image_replacements["{{QP_POLLING_INTERVAL}}"] = str(
                qp_config.get("polling_interval", 10)
            )
            image_replacements["{{QP_MAX_CONCURRENT_JOBS}}"] = str(
                qp_config.get("max_concurrent_jobs", 10)
            )
            image_replacements["{{QP_MESSAGES_PER_JOB}}"] = str(
                qp_config.get("messages_per_job", 1)
            )
            image_replacements["{{QP_SUCCESSFUL_JOBS_HISTORY}}"] = str(
                qp_config.get("successful_jobs_history", 20)
            )
            image_replacements["{{QP_FAILED_JOBS_HISTORY}}"] = str(
                qp_config.get("failed_jobs_history", 10)
            )
            image_replacements["{{QP_ALLOWED_NAMESPACES}}"] = ",".join(
                job_policy.get("allowed_namespaces", ["default", "gco-jobs"])
            )
            # Resource caps, image allowlist, and security policy are shared
            # with the REST manifest processor. Source them from the
            # job_validation_policy section so a single change in cdk.json
            # takes effect on both submission paths at the next deploy.
            image_replacements["{{QP_MAX_GPU_PER_MANIFEST}}"] = str(
                job_quotas.get("max_gpu_per_manifest", 4)
            )
            image_replacements["{{QP_MAX_CPU_PER_MANIFEST}}"] = str(
                job_quotas.get("max_cpu_per_manifest", "10")
            )
            image_replacements["{{QP_MAX_MEMORY_PER_MANIFEST}}"] = str(
                job_quotas.get("max_memory_per_manifest", "32Gi")
            )
            image_replacements["{{QP_TRUSTED_REGISTRIES}}"] = ",".join(
                _augment_trusted_registries_with_project_ecr(
                    job_policy.get("trusted_registries", []),
                    account=self.account,
                    regions=self.config.get_regions(),
                    global_region=self.config.get_global_region(),
                )
            )
            image_replacements["{{QP_TRUSTED_DOCKERHUB_ORGS}}"] = ",".join(
                job_policy.get("trusted_dockerhub_orgs", [])
            )

            # Security policy toggles — shared with the REST manifest_processor.
            # Both services read the same cdk.json section so a single policy
            # flip (e.g. block_run_as_root: true) takes effect on both paths.
            security_policy = job_policy.get("manifest_security_policy", {})

            def _policy_str(v: object) -> str:
                return "true" if v else "false"

            image_replacements["{{QP_BLOCK_PRIVILEGED}}"] = _policy_str(
                security_policy.get("block_privileged", True)
            )
            image_replacements["{{QP_BLOCK_PRIVILEGE_ESCALATION}}"] = _policy_str(
                security_policy.get("block_privilege_escalation", True)
            )
            image_replacements["{{QP_BLOCK_HOST_NETWORK}}"] = _policy_str(
                security_policy.get("block_host_network", True)
            )
            image_replacements["{{QP_BLOCK_HOST_PID}}"] = _policy_str(
                security_policy.get("block_host_pid", True)
            )
            image_replacements["{{QP_BLOCK_HOST_IPC}}"] = _policy_str(
                security_policy.get("block_host_ipc", True)
            )
            image_replacements["{{QP_BLOCK_HOST_PATH}}"] = _policy_str(
                security_policy.get("block_host_path", True)
            )
            image_replacements["{{QP_BLOCK_ADDED_CAPABILITIES}}"] = _policy_str(
                security_policy.get("block_added_capabilities", True)
            )
            image_replacements["{{QP_BLOCK_RUN_AS_ROOT}}"] = _policy_str(
                security_policy.get("block_run_as_root", False)
            )

        # Add Valkey endpoint if enabled
        if hasattr(self, "valkey_cache") and self.valkey_cache:
            image_replacements["{{VALKEY_ENDPOINT}}"] = self.valkey_cache.attr_endpoint_address
            image_replacements["{{VALKEY_PORT}}"] = self.valkey_cache.attr_endpoint_port

        # Add Aurora pgvector endpoint if enabled
        if hasattr(self, "aurora_cluster") and self.aurora_cluster:
            image_replacements["{{AURORA_PGVECTOR_ENDPOINT}}"] = (
                self.aurora_cluster.cluster_endpoint.hostname
            )
            image_replacements["{{AURORA_PGVECTOR_READER_ENDPOINT}}"] = (
                self.aurora_cluster.cluster_read_endpoint.hostname
            )
            image_replacements["{{AURORA_PGVECTOR_PORT}}"] = str(
                self.aurora_cluster.cluster_endpoint.port
            )
            if self.aurora_cluster.secret:
                image_replacements["{{AURORA_PGVECTOR_SECRET_ARN}}"] = (
                    self.aurora_cluster.secret.secret_arn
                )

        # Add FSx replacements if enabled
        if self.fsx_file_system:
            image_replacements["{{FSX_FILE_SYSTEM_ID}}"] = self.fsx_file_system.ref
            image_replacements["{{FSX_DNS_NAME}}"] = self.fsx_file_system.attr_dns_name
            image_replacements["{{FSX_MOUNT_NAME}}"] = self.fsx_file_system.attr_lustre_mount_name
            image_replacements["{{PRIVATE_SUBNET_ID}}"] = self.vpc.private_subnets[0].subnet_id
            image_replacements["{{FSX_SECURITY_GROUP_ID}}"] = (
                self.fsx_security_group.security_group_id
            )

        # ── Trigger the convergence pipeline (fire-and-forget) ───────────────
        # A single custom resource starts the HelmInstallStateMachine, which now
        # owns the WHOLE cluster convergence: apply base manifests -> install
        # Helm charts -> apply post-Helm (CRD-dependent) manifests -> register
        # the Ingress-created ALB with Global Accelerator. The resource returns
        # as soon as the execution is *started* (no isComplete waiter), so the
        # cluster's CloudFormation lifecycle is never bound to the multi-minute
        # add-on convergence — a slow chart can't blow CloudFormation's ~1h
        # custom-resource ceiling and roll back (destroy) the freshly-created
        # cluster. Status lives in SSM and is surfaced via `gco stacks addons
        # status`; re-converge out-of-band with `gco stacks addons install`.
        #
        # The execution input carries everything the state-machine tasks need:
        # chart selection/overrides, the manifest ImageReplacements (for the base
        # and post-Helm kubectl passes), and the Global Accelerator
        # EndpointGroupArn (for the final GA-registration task).
        converge_trigger = CustomResource(
            self,
            "HelmInstallCharts",
            service_token=self.helm_installer_provider.service_token,
            properties={
                "ClusterName": self.cluster.cluster_name,
                "Region": self.deployment_region,
                # Helm chart selection + per-chart value overrides (e.g. Volcano
                # image_registry redirected to the ECR mirror when enabled).
                "EnabledCharts": self._get_enabled_helm_charts(),
                "Charts": self._helm_chart_value_overrides(),
                "KedaOperatorRoleArn": self.keda_operator_role.role_arn,
                # Template substitutions for the base + post-Helm kubectl passes.
                "ImageReplacements": image_replacements,
                # Endpoint group the final task registers the Ingress ALB with.
                "EndpointGroupArn": self.endpoint_group_arn,
                # Project name lets the orchestrator persist the execution input
                # to SSM so `gco stacks addons install` can replay the whole
                # pipeline without reconstructing chart/manifest config.
                "ProjectName": self.config.get_project_name(),
                # Force re-invocation on every deployment (new charts.yaml,
                # manifest, or image) so convergence re-runs end to end.
                "DeploymentTimestamp": deployment_timestamp,
            },
        )

        # The trigger (and therefore the whole convergence pipeline) must run
        # after the cluster, shared storage, managed-addon IRSA patches, and Pod
        # Identity associations exist: the base manifests reference their tokens,
        # and the rollout-restarts at the end of the base pass need the patched
        # service accounts (otherwise the mutating webhook can't inject
        # AWS_ROLE_ARN and the controllers fail with "no EC2 IMDS role found" —
        # PVCs stuck Pending, missing Container Insights metrics; see the
        # UpdateEfsCsiAddonRole resource in _create_efs_csi_driver_addon). These
        # gates previously sat on the synchronous KubectlApplyManifests custom
        # resource; the base apply now lives in the state machine, so the gate
        # moves to the trigger.
        converge_trigger.node.add_dependency(self.cluster)
        converge_trigger.node.add_dependency(self.efs_file_system)
        if self.fsx_file_system:
            converge_trigger.node.add_dependency(self.fsx_file_system)
        for attr in (
            "_efs_csi_addon_role_update",
            "_fsx_csi_addon_role_update",
            "_cloudwatch_addon_role_update",
        ):
            update_cr = getattr(self, attr, None)
            if update_cr is not None:
                converge_trigger.node.add_dependency(update_cr)
        for assoc in self._pod_identity_associations:
            converge_trigger.node.add_dependency(assoc)

    def _create_ga_registration_lambda(self) -> None:
        """Create Lambda function to register Ingress-created ALB with Global Accelerator.

        This Lambda:
        1. Waits for the Ingress to get an ALB address
        2. Gets the ALB ARN from the address
        3. Registers that ALB with Global Accelerator

        This is necessary because the ALB is created by the AWS Load Balancer Controller
        (not CDK), so we can't directly reference its ARN.
        """
        project_name = self.config.get_project_name()

        # Create Lambda function for GA registration using external handler
        ga_registration_lambda = lambda_.Function(
            self,
            "GaRegistrationFunction",
            runtime=getattr(lambda_.Runtime, LAMBDA_PYTHON_RUNTIME),
            handler="handler.lambda_handler",
            code=lambda_.Code.from_asset("lambda/ga-registration"),
            timeout=Duration.minutes(15),  # Max Lambda timeout; handler uses 14 min budget
            memory_size=256,
            vpc=self.vpc,
            vpc_subnets=ec2.SubnetSelection(subnet_type=ec2.SubnetType.PRIVATE_WITH_EGRESS),
            environment={
                "CLUSTER_NAME": self.cluster.cluster_name,
                "REGION": self.deployment_region,
            },
            tracing=lambda_.Tracing.ACTIVE,
        )

        # Grant permissions
        ga_registration_lambda.add_to_role_policy(
            iam.PolicyStatement(
                effect=iam.Effect.ALLOW,
                actions=["eks:DescribeCluster"],
                resources=[self.cluster.cluster_arn],
            )
        )
        ga_registration_lambda.add_to_role_policy(
            iam.PolicyStatement(
                effect=iam.Effect.ALLOW,
                actions=[
                    "elasticloadbalancing:DescribeLoadBalancers",
                    "elasticloadbalancing:DescribeTags",  # Required for tag-based ALB detection
                ],
                resources=["*"],
            )
        )
        ga_registration_lambda.add_to_role_policy(
            iam.PolicyStatement(
                effect=iam.Effect.ALLOW,
                actions=[
                    "globalaccelerator:AddEndpoints",
                    "globalaccelerator:RemoveEndpoints",
                    "globalaccelerator:UpdateEndpointGroup",
                    "globalaccelerator:DescribeEndpointGroup",
                ],
                resources=["*"],
            )
        )
        ga_registration_lambda.add_to_role_policy(
            iam.PolicyStatement(
                effect=iam.Effect.ALLOW,
                actions=["ssm:GetParameter", "ssm:PutParameter", "ssm:DeleteParameter"],
                resources=[
                    f"arn:aws:ssm:{self.config.get_global_region()}:{self.account}:parameter/{project_name}/*"
                ],
            )
        )

        # Add EKS access entry for the Lambda role
        if ga_registration_lambda.role is not None:
            eks.AccessEntry(
                self,
                "GaRegistrationLambdaAccessEntry",
                cluster=self.cluster,  # type: ignore[arg-type]
                principal=ga_registration_lambda.role.role_arn,
                access_policies=[
                    eks.AccessPolicy.from_access_policy_name(
                        "AmazonEKSClusterAdminPolicy", access_scope_type=eks.AccessScopeType.CLUSTER
                    )
                ],
            )

        # Allow Lambda to access EKS API
        self.cluster.cluster_security_group.add_ingress_rule(
            peer=ec2.Peer.ipv4(self.vpc.vpc_cidr_block),
            connection=ec2.Port.tcp(443),
            description="Allow GA registration Lambda to access EKS API",
        )

        # Get endpoint group ARN from SSM (stored in global region).
        # Uses the shared AwsCustomResource execution role (pre-created in
        # _create_aws_custom_resource_role) — the SSM GetParameter
        # statement was attached there up-front so the Lambda never hits
        # an IAM propagation race on cold deploys.
        global_region = self.config.get_global_region()
        get_endpoint_group_arn = cr.AwsCustomResource(
            self,
            "GetEndpointGroupArn",
            on_create=cr.AwsSdkCall(
                service="SSM",
                action="getParameter",
                parameters={"Name": f"/{project_name}/endpoint-group-{self.deployment_region}-arn"},
                region=global_region,
                physical_resource_id=cr.PhysicalResourceId.of(
                    f"{project_name}-get-endpoint-group-arn-{self.deployment_region}"
                ),
            ),
            on_update=cr.AwsSdkCall(
                service="SSM",
                action="getParameter",
                parameters={"Name": f"/{project_name}/endpoint-group-{self.deployment_region}-arn"},
                region=global_region,
            ),
            role=self.aws_custom_resource_role,
        )
        get_endpoint_group_arn.node.add_dependency(self.aws_custom_resource_role)

        endpoint_group_arn = get_endpoint_group_arn.get_response_field("Parameter.Value")

        # No custom-resource provider needed: the GA-registration Lambda is now
        # invoked directly by the convergence state machine's final task.

        # Store for use by the convergence state machine (the GA-registration task).
        self.ga_registration_lambda = ga_registration_lambda
        self.endpoint_group_arn = endpoint_group_arn

        # cdk-nag suppression: the GA registration Lambda needs broad
        # Global Accelerator and ELB Describe access with Resource: *.
        from gco.stacks.nag_suppressions import acknowledge_nag_findings

        acknowledge_nag_findings(
            ga_registration_lambda,
            [
                {
                    "id": "AwsSolutions-IAM5",
                    "reason": (
                        "The GA registration Lambda needs elasticloadbalancing:Describe* "
                        "and globalaccelerator:* to discover the Ingress-created ALB and "
                        "register it with Global Accelerator. These APIs do not support "
                        "resource-level IAM scoping — Resource: * is the only valid form."
                    ),
                    "appliesTo": ["Resource::*"],
                },
            ],
        )

    def _get_volcano_image_mirror_config(self) -> dict[str, Any]:
        """Parse the ``volcano_image_mirror`` block from cdk.json.

        Returns a normalized dict ``{enabled, ecr_namespace}``. Validation is
        strict so a misconfiguration fails at synth rather than silently leaving
        Volcano pointed at docker.io.

        - ``enabled`` (default False) — master toggle.
        - ``ecr_namespace`` (default ``"gco/dockerhub"``) — the ECR repository
          namespace the mirrored Volcano images live under. Must start with
          ``gco/`` so it inherits the project's existing ``gco/*`` machinery
          (node pull access, replication rule, trusted-registry allow-list)
          with no extra IAM, and must be a valid (possibly nested) ECR
          repository path.
        """
        raw = self.node.try_get_context("volcano_image_mirror") or {}
        if not isinstance(raw, dict):
            raise ValueError(f"volcano_image_mirror must be a mapping, got {type(raw).__name__}")

        enabled = bool(raw.get("enabled", False))
        ecr_namespace = str(raw.get("ecr_namespace", "gco/dockerhub")).strip().strip("/")

        if not enabled:
            return {"enabled": False, "ecr_namespace": ecr_namespace}

        # Must live under the project prefix and be a valid nested ECR repo
        # path (lowercase alphanumerics + . _ - per segment, slash-separated).
        if not ecr_namespace.startswith("gco/"):
            raise ValueError(
                "volcano_image_mirror.ecr_namespace must start with 'gco/' so it "
                "inherits the project's gco/* ECR access/replication, got "
                f"{ecr_namespace!r}"
            )
        segment = r"[a-z0-9]+(?:[._-][a-z0-9]+)*"
        if not re.fullmatch(rf"{segment}(?:/{segment})+", ecr_namespace):
            raise ValueError(
                "volcano_image_mirror.ecr_namespace must be a valid ECR repository "
                f"path (lowercase alphanumerics + . _ - per slash-separated segment), "
                f"got {ecr_namespace!r}"
            )

        return {"enabled": True, "ecr_namespace": ecr_namespace}

    def _configure_volcano_image_mirror(self) -> None:
        """Resolve the optional Volcano image-mirror registry (no infra).

        Volcano is the only default chart whose images live exclusively on
        docker.io (``volcanosh/vc-*``). On a cold EKS Auto Mode cluster those
        anonymous pulls are slow / rate-limited, so Volcano's blocking
        ``helm --wait`` could never finish inside the installer Lambda's
        wall-clock guard and the whole add-on batch looped on it.

        The fix is to mirror Volcano's pinned images into the project's own ECR
        under ``gco/*`` and point Volcano's ``basic.image_registry`` there, so
        the cluster makes fast, same-account ECR pulls with the pull-only node
        role it already has — no Docker Hub credential, no pull-through cache
        rule, and no registry permissions policy. The mirror itself is populated
        out-of-band (``gco images mirror``) before the add-ons
        converge; this method only computes the registry override and creates no
        CloudFormation resources.

        Sets ``self.volcano_mirror_registry`` to
        ``<account>.dkr.ecr.<region>.amazonaws.com/<ecr_namespace>`` that
        ``_helm_chart_value_overrides`` feeds into Volcano's
        ``basic.image_registry``; left ``None`` when disabled.
        """
        # Always define the attribute so downstream code can branch on it.
        self.volcano_mirror_registry: str | None = None

        cfg = self._get_volcano_image_mirror_config()
        if not cfg["enabled"]:
            return

        ecr_namespace = cfg["ecr_namespace"]
        self.volcano_mirror_registry = (
            f"{self.account}.dkr.ecr.{self.deployment_region}.amazonaws.com/{ecr_namespace}"
        )

    def _helm_chart_value_overrides(self) -> dict[str, Any]:
        """Per-chart helm value overrides injected into the install payload.

        Returned dict is forwarded verbatim as the ``Charts`` property of the
        ``HelmInstallCharts`` custom resource; the installer deep-merges each
        chart's ``values`` over ``charts.yaml``. Currently used only to point
        Volcano's ``basic.image_registry`` at the project's ECR image mirror
        when enabled, so every Volcano image (controller, scheduler, admission
        webhook, and the pre-install admission-init hook — all of which render
        from ``basic.image_registry``) resolves from ECR instead of docker.io.
        The upstream image names (``volcanosh/vc-*``) are preserved, so each
        resolves to ``<mirror_registry>/volcanosh/vc-*`` — matching where
        ``gco images mirror`` pushes them. Returns ``{}`` when
        the mirror is disabled, preserving the prior behavior.
        """
        if not getattr(self, "volcano_mirror_registry", None):
            return {}
        return {
            "volcano": {
                "values": {
                    "basic": {
                        "image_registry": self.volcano_mirror_registry,
                    }
                }
            }
        }

    def _get_enabled_helm_charts(self) -> list[str]:
        """Return the list of Helm charts to install based on cdk.json helm config.

        Reads the 'helm' section from cdk.json context. Each key maps to one or
        more Helm chart names. Charts are returned in dependency order with Kueue
        last (its webhook intercepts all Job/Deployment mutations).
        """
        helm_config = self.node.try_get_context("helm") or {}

        # Mapping from cdk.json helm key → Helm chart name(s) in charts.yaml
        # Order matters: dependencies first, Kueue last
        chart_map: list[tuple[str, list[str]]] = [
            ("keda", ["keda"]),
            ("aws_efa_device_plugin", ["aws-efa-device-plugin"]),
            ("aws_neuron_device_plugin", ["aws-neuron-device-plugin"]),
            ("volcano", ["volcano"]),
            ("kuberay", ["kuberay-operator"]),
            ("cert_manager", ["cert-manager"]),
            ("slurm", ["slinky-slurm-operator", "slinky-slurm"]),
            ("yunikorn", ["yunikorn"]),
            ("kueue", ["kueue"]),  # Must be last
        ]

        # Charts that are mandatory platform components and cannot be disabled
        # via cdk.json. KEDA is always installed: it backs the built-in SQS
        # queue processor (a ScaledJob) and is the only metrics bridge that lets
        # autoscalers consume GPU/CloudWatch metrics (the keda-metrics-apiserver
        # serves external.metrics.k8s.io). Disabling it would silently break
        # both, so the cdk.json toggle is ignored for KEDA.
        mandatory_chart_keys = {"keda"}

        enabled_charts = []
        for config_key, chart_names in chart_map:
            chart_config = helm_config.get(config_key, {})
            if config_key in mandatory_chart_keys or chart_config.get("enabled", True):
                enabled_charts.extend(chart_names)

        return enabled_charts

    def _create_helm_installer_lambda(self) -> None:
        """Create Lambda function to install Helm charts (KEDA, NVIDIA DRA, etc.).

        This Lambda uses Helm to install charts that require complex setup
        (TLS certificates, CRDs, etc.) that are difficult to manage via raw manifests.

        Charts installed:
        - KEDA: Kubernetes Event-Driven Autoscaling (mandatory, always installed)
        - Volcano, KubeRay, Kueue, cert-manager, and other schedulers (toggle via cdk.json)
        """
        project_name = self.config.get_project_name()

        # Create IAM role for Helm installer Lambda
        helm_lambda_role = iam.Role(
            self,
            "HelmInstallerLambdaRole",
            assumed_by=iam.ServicePrincipal("lambda.amazonaws.com"),
            managed_policies=[
                iam.ManagedPolicy.from_aws_managed_policy_name(
                    "service-role/AWSLambdaVPCAccessExecutionRole"
                ),
                iam.ManagedPolicy.from_aws_managed_policy_name(
                    "service-role/AWSLambdaBasicExecutionRole"
                ),
            ],
        )

        # Add EKS permissions
        helm_lambda_role.add_to_policy(
            iam.PolicyStatement(
                actions=["eks:DescribeCluster", "eks:ListClusters"],
                resources=[self.cluster.cluster_arn],
            )
        )

        # Create security group for Helm installer Lambda
        helm_lambda_sg = ec2.SecurityGroup(
            self,
            "HelmInstallerLambdaSG",
            vpc=self.vpc,
            description="Security group for Helm installer Lambda to access EKS cluster",
            security_group_name=f"{project_name}-helm-lambda-sg-{self.deployment_region}",
            allow_all_outbound=True,
        )

        # Allow Lambda to access EKS cluster API
        self.cluster.cluster_security_group.add_ingress_rule(
            peer=helm_lambda_sg,
            connection=ec2.Port.tcp(443),
            description="Allow Helm installer Lambda to access EKS API",
        )

        # Build Docker image for Helm installer Lambda
        # Points at helm-installer-build/ which is rebuilt fresh every deploy
        # by _build_helm_installer_lambda() in cli/stacks.py
        ecr_assets.DockerImageAsset(
            self,
            "HelmInstallerImage",
            directory="lambda/helm-installer-build",
            platform=ecr_assets.Platform.LINUX_AMD64,
        )

        # Create Lambda function using Docker image
        # Store function name as string attribute for cross-stack references
        # This avoids CDK cross-environment resolution issues when account is unresolved
        self.helm_installer_lambda_function_name = f"{project_name}-helm-{self.deployment_region}"
        self.helm_installer_lambda = lambda_.DockerImageFunction(
            self,
            "HelmInstallerFunction",
            function_name=self.helm_installer_lambda_function_name,
            code=lambda_.DockerImageCode.from_image_asset(
                directory="lambda/helm-installer-build",
                platform=ecr_assets.Platform.LINUX_AMD64,
            ),
            timeout=Duration.minutes(15),
            memory_size=1024,
            architecture=lambda_.Architecture.X86_64,
            role=helm_lambda_role,
            vpc=self.vpc,
            vpc_subnets=ec2.SubnetSelection(subnet_type=ec2.SubnetType.PRIVATE_WITH_EGRESS),
            security_groups=[helm_lambda_sg],
            environment={
                "CLUSTER_NAME": self.cluster.cluster_name,
                "REGION": self.deployment_region,
                "PROJECT_NAME": project_name,
            },
            tracing=lambda_.Tracing.ACTIVE,
        )

        # Allow the installer to record per-chart add-on status to SSM so the
        # add-on layer's health is observable out-of-band (decoupled from the
        # CloudFormation rollback path). Read back via `gco stacks addons-status`.
        helm_lambda_role.add_to_policy(
            iam.PolicyStatement(
                actions=["ssm:PutParameter"],
                resources=[
                    f"arn:aws:ssm:{self.deployment_region}:{self.account}:"
                    f"parameter/{project_name}/addons/*"
                ],
            )
        )

        # Add EKS access entry for the Lambda role
        eks.AccessEntry(
            self,
            "HelmInstallerLambdaAccessEntry",
            cluster=self.cluster,  # type: ignore[arg-type]
            principal=helm_lambda_role.role_arn,
            access_policies=[
                eks.AccessPolicy.from_access_policy_name(
                    "AmazonEKSClusterAdminPolicy", access_scope_type=eks.AccessScopeType.CLUSTER
                )
            ],
        )

        # ------------------------------------------------------------------
        # Step Functions state machine: one task per chart, in charts.yaml
        # order. Each chart gets its own retry + Step Functions console
        # visibility, and — critically — no single Lambda invocation is bound
        # by the 15-minute Lambda limit, so a slow operator (e.g. a cold NVIDIA
        # image pull) just costs extra retries instead of failing the deploy.
        # ------------------------------------------------------------------
        chart_order = _load_helm_chart_order()

        def _chart_task(chart_name: str) -> sfn_tasks.LambdaInvoke:
            task = sfn_tasks.LambdaInvoke(
                self,
                f"HelmChart-{chart_name}",
                lambda_function=self.helm_installer_lambda,
                payload=sfn.TaskInput.from_object(
                    {
                        "Action": "install_chart",
                        "Chart": chart_name,
                        "ClusterName": sfn.JsonPath.string_at("$.ClusterName"),
                        "Region": sfn.JsonPath.string_at("$.Region"),
                        "EnabledCharts": sfn.JsonPath.list_at("$.EnabledCharts"),
                        "Charts": sfn.JsonPath.object_at("$.Charts"),
                        "KedaOperatorRoleArn": sfn.JsonPath.string_at("$.KedaOperatorRoleArn"),
                    }
                ),
                payload_response_only=True,
                # Keep the execution input intact so the next chart task can
                # still read $.ClusterName, $.EnabledCharts, etc.
                result_path="$.lastChart",
                task_timeout=sfn.Timeout.duration(Duration.minutes(16)),
            )
            # Per-chart retry with backoff. A cold image pull or a webhook race
            # clears on a later attempt; only after exhausting these does the
            # chart (and the deploy) fail.
            task.add_retry(
                errors=["States.ALL"],
                max_attempts=4,
                interval=Duration.seconds(30),
                backoff_rate=2.0,
                max_delay=Duration.minutes(5),
            )
            return task

        def _kubectl_task(task_id: str, *, post_helm: bool) -> sfn_tasks.LambdaInvoke:
            """One kubectl-apply pass (base or post-Helm) as a state-machine task.

            Reads ClusterName / Region / ImageReplacements from the execution
            input; the handler raises on any manifest failure so the task's
            Retry/Catch can react.
            """
            task = sfn_tasks.LambdaInvoke(
                self,
                task_id,
                lambda_function=self.kubectl_lambda,
                payload=sfn.TaskInput.from_object(
                    {
                        "Action": "apply_manifests",
                        "ClusterName": sfn.JsonPath.string_at("$.ClusterName"),
                        "Region": sfn.JsonPath.string_at("$.Region"),
                        "ImageReplacements": sfn.JsonPath.object_at("$.ImageReplacements"),
                        "PostHelm": "true" if post_helm else "false",
                    }
                ),
                payload_response_only=True,
                # Keep the execution input intact so later tasks still read
                # $.ClusterName, $.ImageReplacements, $.EndpointGroupArn, etc.
                result_path="$.lastApply",
                task_timeout=sfn.Timeout.duration(Duration.minutes(15)),
            )
            task.add_retry(
                errors=["States.ALL"],
                max_attempts=3,
                interval=Duration.seconds(30),
                backoff_rate=2.0,
                max_delay=Duration.minutes(3),
            )
            return task

        def _ga_task() -> sfn_tasks.LambdaInvoke:
            """Register the Ingress-created ALB with Global Accelerator (final step).

            The handler polls for the active ALB (up to a 14-minute budget), so
            this task gets a 16-minute timeout to cover the Lambda's full run.
            """
            task = sfn_tasks.LambdaInvoke(
                self,
                "RegisterGlobalAccelerator",
                lambda_function=self.ga_registration_lambda,
                payload=sfn.TaskInput.from_object(
                    {
                        "Action": "register_ga",
                        "ClusterName": sfn.JsonPath.string_at("$.ClusterName"),
                        "Region": sfn.JsonPath.string_at("$.Region"),
                        "EndpointGroupArn": sfn.JsonPath.string_at("$.EndpointGroupArn"),
                        "IngressName": "gco-ingress",
                        "Namespace": "gco-system",
                        "GlobalRegion": self.config.get_global_region(),
                        "ProjectName": project_name,
                    }
                ),
                payload_response_only=True,
                result_path="$.gaRegistration",
                task_timeout=sfn.Timeout.duration(Duration.minutes(16)),
            )
            task.add_retry(
                errors=["States.ALL"],
                max_attempts=3,
                interval=Duration.seconds(30),
                backoff_rate=2.0,
                max_delay=Duration.minutes(3),
            )
            return task

        chart_tasks = [_chart_task(name) for name in chart_order]

        # The state machine owns the full convergence pipeline:
        #   base kubectl apply -> Helm charts -> post-Helm kubectl apply -> GA.
        # Failure policy:
        #   * base apply: retry, then NO catch -> a failed base pass (missing
        #     namespaces / RBAC / storage / nodepools) FAILS the execution;
        #     nothing downstream should run against a half-applied base.
        #   * charts: each chart's success AND its post-retry failure both route
        #     to the next, so one slow/broken chart never blocks the rest.
        #   * post-Helm apply + GA: retry, then catch-and-continue, so a single
        #     CRD-dependent manifest or a GA hiccup can't wedge the rest.
        # Per-task outcomes are recorded to SSM; the trigger is fire-and-forget,
        # so a degraded add-on layer never rolls back the cluster.
        done = sfn.Succeed(self, "HelmInstallComplete")
        base_apply = _kubectl_task("ApplyBaseManifests", post_helm=False)
        post_apply = _kubectl_task("ApplyPostHelmManifests", post_helm=True)
        ga_task = _ga_task()

        # post-Helm apply -> GA -> done, both catch-and-continue.
        post_apply.add_catch(ga_task, errors=["States.ALL"], result_path="$.lastApplyError")
        post_apply.next(ga_task)
        ga_task.add_catch(done, errors=["States.ALL"], result_path="$.gaError")
        ga_task.next(done)

        if chart_tasks:
            for i, task in enumerate(chart_tasks):
                next_state: sfn.IChainable = (
                    chart_tasks[i + 1] if i + 1 < len(chart_tasks) else post_apply
                )
                task.add_catch(
                    next_state,
                    errors=["States.ALL"],
                    result_path="$.lastChartError",
                )
                task.next(next_state)
            base_apply.next(chart_tasks[0])
        else:  # pragma: no cover - charts.yaml is always present in the repo
            base_apply.next(post_apply)

        # base apply has NO catch on purpose: a persistent base-manifest failure
        # fails the execution rather than converging onto an incomplete base.
        start_state: sfn.IChainable = base_apply

        helm_sm_log_group = logs.LogGroup(
            self,
            "HelmInstallStateMachineLogGroup",
            retention=logs.RetentionDays.ONE_WEEK,
            removal_policy=RemovalPolicy.DESTROY,
        )

        self.helm_install_state_machine = sfn.StateMachine(
            self,
            "HelmInstallStateMachine",
            definition_body=sfn.DefinitionBody.from_chainable(start_state),
            state_machine_type=sfn.StateMachineType.STANDARD,
            timeout=Duration.hours(2),
            tracing_enabled=True,
            logs=sfn.LogOptions(destination=helm_sm_log_group, level=sfn.LogLevel.ALL),
        )

        # Thin fire-and-forget provider: onEvent starts the execution and
        # returns immediately. It does no Helm/Kubernetes work, so it never
        # approaches the Lambda timeout — all the heavy lifting lives in the
        # state machine, which converges charts in the background.
        helm_orchestrator_on_event = lambda_.Function(
            self,
            "HelmOrchestratorOnEvent",
            runtime=getattr(lambda_.Runtime, LAMBDA_PYTHON_RUNTIME),
            handler="handler.on_event",
            code=lambda_.Code.from_asset("lambda/helm-orchestrator"),
            timeout=Duration.minutes(1),
            memory_size=256,
            environment={
                "STATE_MACHINE_ARN": self.helm_install_state_machine.state_machine_arn,
            },
        )
        self.helm_install_state_machine.grant_start_execution(helm_orchestrator_on_event)

        # Let on_event persist the execution input to SSM so the add-on install
        # can be replayed out-of-band (gco stacks addons install) without the
        # CLI reconstructing chart config or the KEDA role ARN.
        helm_orchestrator_on_event.add_to_role_policy(
            iam.PolicyStatement(
                actions=["ssm:PutParameter"],
                resources=[
                    f"arn:aws:ssm:{self.deployment_region}:{self.account}:"
                    f"parameter/{project_name}/addons/*"
                ],
            )
        )

        # Create log group for the custom-resource provider framework
        helm_provider_log_group = logs.LogGroup(
            self,
            "HelmInstallerProviderLogGroup",
            retention=logs.RetentionDays.ONE_WEEK,
            removal_policy=RemovalPolicy.DESTROY,
        )

        # Fire-and-forget custom-resource provider: onEvent starts the helm
        # install state machine and the resource completes immediately. The
        # cluster's CloudFormation lifecycle is deliberately NOT bound to the
        # helm batch finishing — a slow chart (e.g. volcano retrying docker.io
        # pulls) would otherwise keep the execution RUNNING past CloudFormation's
        # ~1h custom-resource ceiling and trigger a rollback that destroys the
        # cluster. Charts converge in the background; status lives in SSM and is
        # surfaced via `gco stacks addons status` / re-converged with
        # `gco stacks addons install`. No isComplete handler => no waiter.
        self.helm_installer_provider = cr.Provider(
            self,
            "HelmInstallerProvider",
            on_event_handler=helm_orchestrator_on_event,
            log_group=helm_provider_log_group,
        )

        # cdk-nag suppressions for the install path.
        from gco.stacks.nag_suppressions import acknowledge_nag_findings

        acknowledge_nag_findings(
            helm_lambda_role,
            [
                {
                    "id": "AwsSolutions-IAM5",
                    "reason": (
                        "The Helm installer Lambda requires broad EKS and Kubernetes API "
                        "access to install Helm charts (KEDA, NVIDIA DRA, etc.) that create "
                        "CRDs, RBAC rules, and workloads across multiple namespaces. "
                        "Resource: * is required because the set of Kubernetes resources "
                        "is dynamic and not known at synth time."
                    ),
                    "appliesTo": ["Resource::*"],
                },
            ],
        )
        # The state machine role (auto-generated) invokes the worker Lambda
        # across versions using the AWS-standard ``:*`` qualifier that cannot be
        # enumerated at synth time.
        acknowledge_nag_findings(
            self.helm_install_state_machine,
            [
                {
                    "id": "AwsSolutions-IAM5",
                    "reason": (
                        "The state machine invokes the helm worker Lambda; CDK grants "
                        "lambda:InvokeFunction with the :* version qualifier, which is the "
                        "standard form and cannot be narrowed at synth time."
                    ),
                    "appliesTo": ["Resource::*"],
                },
            ],
        )
        acknowledge_nag_findings(
            helm_orchestrator_on_event,
            [
                {
                    "id": "AwsSolutions-IAM5",
                    "reason": (
                        "Start on the helm-install state machine requires the "
                        "execution-ARN wildcard (arn:...:execution:<sm>:*) because "
                        "execution names are generated at runtime. The finding is a "
                        "granular execution-ARN wildcard that cannot be enumerated at "
                        "synth time, so no appliesTo filter is supplied."
                    ),
                },
            ],
        )

        # The cr.Provider framework auto-generates a framework-onEvent Lambda and
        # its role (and, were an is_complete_handler set, a waiter state machine —
        # which this fire-and-forget provider does NOT create). None of these are
        # configurable by us: the framework role invokes our handler Lambda via
        # the standard ``<lambda-arn>:*`` version qualifier that cannot be narrowed
        # at synth time. Suppress the relevant rules across the whole provider
        # subtree; appliesTo is omitted because the findings are granted on
        # CDK-managed resources we do not author. The SF1/SF2/X-Ray entries are
        # retained defensively to cover any helper state machine the framework may
        # emit across CDK versions; they are harmless no-ops when none exists.
        acknowledge_nag_findings(
            self.helm_installer_provider,
            [
                {
                    "id": "AwsSolutions-IAM5",
                    "reason": (
                        "CDK custom-resource provider framework roles invoke the "
                        "orchestrator Lambdas via the standard '<lambda-arn>:*' version "
                        "qualifier, which cannot be enumerated at synth time."
                    ),
                    "appliesTo": [
                        "Resource::<HelmOrchestratorOnEventD0D51D9B.Arn>:*",
                    ],
                },
                {
                    "id": "AwsSolutions-SF1",
                    "reason": (
                        "The waiter state machine is auto-generated by the CDK "
                        "cr.Provider framework and does not expose log configuration; "
                        "ALL-event logging cannot be enabled on it."
                    ),
                },
                {
                    "id": "AwsSolutions-SF2",
                    "reason": (
                        "The waiter state machine is auto-generated by the CDK "
                        "cr.Provider framework and does not expose tracing "
                        "configuration; X-Ray cannot be enabled on it."
                    ),
                },
                {
                    "id": "Serverless-StepFunctionStateMachineXray",
                    "reason": (
                        "The waiter state machine is auto-generated by the CDK "
                        "cr.Provider framework and does not expose tracing "
                        "configuration; X-Ray cannot be enabled on it."
                    ),
                },
            ],
        )

    def _create_efs(self) -> None:
        """Create EFS file system for shared storage across jobs.

        Creates an EFS file system with mount targets in each private subnet,
        allowing pods to share data and persist outputs. The EFS is configured
        with:
        - Encryption at rest
        - Automatic backups
        - General Purpose performance mode (suitable for most workloads)
        - Bursting throughput mode

        Kubernetes resources (StorageClass, PV, PVC) are created via manifests.
        """
        project_name = self.config.get_project_name()

        # Create security group for EFS
        self.efs_security_group = ec2.SecurityGroup(
            self,
            "EfsSecurityGroup",
            vpc=self.vpc,
            description=f"Security group for {project_name} EFS in {self.deployment_region}",
            security_group_name=f"{project_name}-efs-sg-{self.deployment_region}",
            allow_all_outbound=False,  # EFS doesn't need outbound
        )

        # Allow NFS traffic from EKS cluster security group
        self.efs_security_group.add_ingress_rule(
            peer=self.cluster.cluster_security_group,
            connection=ec2.Port.tcp(2049),
            description="Allow NFS from EKS cluster",
        )

        # Create EFS file system
        self.efs_file_system = efs.FileSystem(
            self,
            "GCOEfs",
            vpc=self.vpc,
            file_system_name=f"{project_name}-efs-{self.deployment_region}",
            security_group=self.efs_security_group,
            vpc_subnets=ec2.SubnetSelection(subnet_type=ec2.SubnetType.PRIVATE_WITH_EGRESS),
            encrypted=True,
            performance_mode=efs.PerformanceMode.GENERAL_PURPOSE,
            throughput_mode=efs.ThroughputMode.BURSTING,
            removal_policy=RemovalPolicy.DESTROY,  # For dev/test; use RETAIN for production
            enable_automatic_backups=True,
        )

        # Add file system policy to allow mounting without IAM authorization
        # This allows any client that can reach the mount target to mount the file system
        self.efs_file_system.add_to_resource_policy(
            iam.PolicyStatement(
                effect=iam.Effect.ALLOW,
                principals=[iam.AnyPrincipal()],
                actions=[
                    "elasticfilesystem:ClientMount",
                    "elasticfilesystem:ClientWrite",
                    "elasticfilesystem:ClientRootAccess",
                ],
                conditions={"Bool": {"elasticfilesystem:AccessedViaMountTarget": "true"}},
            )
        )

        # Create access point for the gco-jobs directory
        self.efs_access_point = self.efs_file_system.add_access_point(
            "JobsAccessPoint",
            path="/gco-jobs",
            create_acl=efs.Acl(owner_uid="1000", owner_gid="1000", permissions="755"),
            posix_user=efs.PosixUser(uid="1000", gid="1000"),
        )

        # Output EFS information
        CfnOutput(
            self,
            "EfsFileSystemId",
            value=self.efs_file_system.file_system_id,
            description="EFS File System ID for shared job storage",
        )

        CfnOutput(
            self,
            "EfsAccessPointId",
            value=self.efs_access_point.access_point_id,
            description="EFS Access Point ID for job outputs",
        )

    def _create_fsx_lustre(self) -> None:
        """Create FSx for Lustre file system for high-performance storage.

        FSx for Lustre provides high-performance parallel file system storage
        ideal for ML training workloads that require high throughput and low latency.

        This is optional and controlled by the fsx_lustre.enabled config setting.

        Supported deployment types:
        - SCRATCH_1: Temporary storage, no data replication
        - SCRATCH_2: Temporary storage with better burst performance
        - PERSISTENT_1: Persistent storage with data replication
        - PERSISTENT_2: Latest persistent storage with higher throughput
        """
        fsx_config = self.config.get_fsx_lustre_config(self.deployment_region)

        if not fsx_config.get("enabled", False):
            self.fsx_file_system = None
            return

        project_name = self.config.get_project_name()

        # Create security group for FSx
        self.fsx_security_group = ec2.SecurityGroup(
            self,
            "FsxSecurityGroup",
            vpc=self.vpc,
            description=f"Security group for {project_name} FSx Lustre in {self.deployment_region}",
            security_group_name=f"{project_name}-fsx-sg-{self.deployment_region}",
            allow_all_outbound=False,
        )

        # Allow Lustre traffic from EKS cluster security group
        # Lustre uses ports 988 (control) and 1021-1023 (data)
        self.fsx_security_group.add_ingress_rule(
            peer=self.cluster.cluster_security_group,
            connection=ec2.Port.tcp(988),
            description="Allow Lustre control traffic from EKS cluster",
        )
        self.fsx_security_group.add_ingress_rule(
            peer=self.cluster.cluster_security_group,
            connection=ec2.Port.tcp_range(1021, 1023),
            description="Allow Lustre data traffic from EKS cluster",
        )

        # Allow self-referencing traffic for FSx Lustre internal communication
        # FSx Lustre nodes need to communicate with each other on port 988
        self.fsx_security_group.add_ingress_rule(
            peer=self.fsx_security_group,
            connection=ec2.Port.tcp(988),
            description="Allow Lustre internal traffic on port 988",
        )
        self.fsx_security_group.add_ingress_rule(
            peer=self.fsx_security_group,
            connection=ec2.Port.tcp_range(1021, 1023),
            description="Allow Lustre internal traffic on ports 1021-1023",
        )

        # Get deployment type
        deployment_type = fsx_config.get("deployment_type", "SCRATCH_2")
        storage_capacity = fsx_config.get("storage_capacity_gib", 1200)

        # Build Lustre configuration based on deployment type
        lustre_config = {
            "deploymentType": deployment_type,
            "dataCompressionType": fsx_config.get("data_compression_type", "LZ4"),
        }

        # Add throughput for PERSISTENT types
        if deployment_type.startswith("PERSISTENT"):
            lustre_config["perUnitStorageThroughput"] = fsx_config.get(
                "per_unit_storage_throughput", 200
            )

        # Add S3 import/export if configured
        import_path = fsx_config.get("import_path")
        export_path = fsx_config.get("export_path")

        if import_path:
            lustre_config["importPath"] = import_path
            lustre_config["autoImportPolicy"] = fsx_config.get(
                "auto_import_policy", "NEW_CHANGED_DELETED"
            )

        if export_path:
            lustre_config["exportPath"] = export_path

        # Get file system type version (default to 2.15 for kernel 6.x compatibility)
        # IMPORTANT: Lustre 2.10 is NOT compatible with kernel 6.x (AL2023, Bottlerocket 1.19+)
        # See: https://docs.aws.amazon.com/fsx/latest/LustreGuide/lustre-client-matrix.html
        file_system_type_version = fsx_config.get("file_system_type_version", "2.15")

        # Create FSx for Lustre file system
        self.fsx_file_system = fsx.CfnFileSystem(
            self,
            "GCOFsxLustre",
            file_system_type="LUSTRE",
            file_system_type_version=file_system_type_version,
            storage_capacity=storage_capacity,
            subnet_ids=[self.vpc.private_subnets[0].subnet_id],
            security_group_ids=[self.fsx_security_group.security_group_id],
            lustre_configuration=lustre_config,
            tags=[
                {"key": "Name", "value": f"{project_name}-fsx-{self.deployment_region}"},
                {"key": "Project", "value": project_name},
            ],
        )

        # Ensure FSx file system waits for security group ingress rules to be created
        # This prevents "security group does not permit Lustre LNET traffic" errors
        self.fsx_file_system.node.add_dependency(self.fsx_security_group)

        # Create FSx CSI Driver add-on for Kubernetes integration
        self._create_fsx_csi_driver_addon()

        # Output FSx information
        CfnOutput(
            self,
            "FsxFileSystemId",
            value=self.fsx_file_system.ref,
            description="FSx for Lustre File System ID",
        )

        CfnOutput(
            self,
            "FsxDnsName",
            value=self.fsx_file_system.attr_dns_name,
            description="FSx for Lustre DNS Name",
        )

        CfnOutput(
            self,
            "FsxMountName",
            value=self.fsx_file_system.attr_lustre_mount_name,
            description="FSx for Lustre Mount Name",
        )

    def _create_valkey_cache(self) -> None:
        """Create an ElastiCache Serverless Valkey cache for K/V caching.

        Provides a low-latency key-value store that inference endpoints and
        jobs can use for prompt caching, session state, feature stores, or
        any shared state across pods.  Valkey Serverless auto-scales and
        requires no node management.

        The cache is placed in the VPC private subnets and accessible from
        any pod via the cluster security group.
        """
        valkey_config = self.config.get_valkey_config()
        if not valkey_config.get("enabled", False):
            return

        from aws_cdk import aws_elasticache as elasticache

        # Security group for Valkey (allow access from EKS cluster)
        valkey_sg = ec2.SecurityGroup(
            self,
            "ValkeySG",
            vpc=self.vpc,
            description="Security group for Valkey Serverless cache",
            allow_all_outbound=False,
        )
        valkey_sg.add_ingress_rule(
            ec2.Peer.ipv4(self.vpc.vpc_cidr_block),
            ec2.Port.tcp(6379),
            "Allow Valkey access from VPC",
        )

        # The Valkey SG ingress allows 6379 from the VPC CIDR (an ``Fn::GetAtt``
        # token cdk-nag can't resolve), so the SG-ingress rules throw. Scope the
        # acknowledgment to the Valkey SG construct itself.
        from gco.stacks.nag_suppressions import acknowledge_security_group_cidr_findings

        acknowledge_security_group_cidr_findings(
            valkey_sg,
            reason=(
                "The Valkey Serverless cache security group allows the Valkey "
                "port (6379) from the VPC CIDR only, referenced via an "
                "``Fn::GetAtt`` token that cdk-nag cannot resolve at synth "
                "time. Ingress is restricted to intra-VPC traffic from the "
                "job pods that use the cache."
            ),
        )

        private_subnet_ids = [s.subnet_id for s in self.vpc.private_subnets]

        self.valkey_cache = elasticache.CfnServerlessCache(
            self,
            "ValkeyCache",
            engine="valkey",
            serverless_cache_name=f"gco-{self.deployment_region}",
            description=f"GCO K/V cache for {self.deployment_region}",
            major_engine_version="8",
            security_group_ids=[valkey_sg.security_group_id],
            subnet_ids=private_subnet_ids,
            cache_usage_limits=elasticache.CfnServerlessCache.CacheUsageLimitsProperty(
                data_storage=elasticache.CfnServerlessCache.DataStorageProperty(
                    maximum=valkey_config.get("max_data_storage_gb", 5),
                    minimum=1,
                    unit="GB",
                ),
                ecpu_per_second=elasticache.CfnServerlessCache.ECPUPerSecondProperty(
                    maximum=valkey_config.get("max_ecpu_per_second", 5000),
                    minimum=1000,
                ),
            ),
            snapshot_retention_limit=valkey_config.get("snapshot_retention_limit", 1),
            tags=[
                CfnTag(key="Project", value="gco"),
                CfnTag(key="Region", value=self.deployment_region),
            ],
        )

        CfnOutput(
            self,
            "ValkeyEndpoint",
            value=self.valkey_cache.attr_endpoint_address,
            description="Valkey Serverless cache endpoint",
        )
        CfnOutput(
            self,
            "ValkeyPort",
            value=self.valkey_cache.attr_endpoint_port,
            description="Valkey Serverless cache port",
        )

        # Store endpoint in SSM for discovery by pods
        ssm.StringParameter(
            self,
            "ValkeyEndpointParam",
            parameter_name=f"/{self.config.get_project_name()}/valkey-endpoint-{self.deployment_region}",
            string_value=self.valkey_cache.attr_endpoint_address,
            description=f"Valkey endpoint for {self.deployment_region}",
        )

    def _create_aurora_pgvector(self) -> None:
        """Create an Aurora Serverless v2 PostgreSQL cluster with pgvector.

        Provides a fully managed vector database that inference endpoints and
        jobs can use for RAG (retrieval-augmented generation), semantic search,
        embedding storage, and similarity queries. Aurora Serverless v2
        auto-scales capacity and requires no instance management.

        The cluster is placed in the VPC private subnets and accessible from
        any pod via the cluster security group. Credentials are stored in
        Secrets Manager and the endpoint is published to SSM + a K8s ConfigMap
        for automatic discovery.

        See: https://aws.amazon.com/blogs/database/accelerate-generative-ai-workloads-on-amazon-aurora-with-optimized-reads-and-pgvector/
        """
        aurora_config = self.config.get_aurora_pgvector_config()
        if not aurora_config.get("enabled", False):
            return

        from aws_cdk import aws_rds as rds

        project_name = self.config.get_project_name()

        # Security group for Aurora (allow PostgreSQL access from EKS cluster only)
        aurora_sg = ec2.SecurityGroup(
            self,
            "AuroraPgvectorSG",
            vpc=self.vpc,
            description="Security group for Aurora Serverless v2 pgvector",
            allow_all_outbound=False,
        )
        aurora_sg.add_ingress_rule(
            self.cluster.cluster_security_group,
            ec2.Port.tcp(5432),
            "Allow PostgreSQL access from EKS cluster",
        )

        # Subnet group for Aurora (private subnets only)
        subnet_group = rds.SubnetGroup(
            self,
            "AuroraPgvectorSubnetGroup",
            description=f"Subnet group for GCO Aurora pgvector in {self.deployment_region}",
            vpc=self.vpc,
            vpc_subnets=ec2.SubnetSelection(subnet_type=ec2.SubnetType.PRIVATE_WITH_EGRESS),
        )

        # Aurora Serverless v2 cluster with PostgreSQL 16 + pgvector
        self.aurora_cluster = rds.DatabaseCluster(
            self,
            "AuroraPgvectorCluster",
            engine=rds.DatabaseClusterEngine.aurora_postgres(
                version=getattr(rds.AuroraPostgresEngineVersion, AURORA_POSTGRES_VERSION),
            ),
            serverless_v2_min_capacity=aurora_config.get("min_acu", 0),
            serverless_v2_max_capacity=aurora_config.get("max_acu", 16),
            writer=rds.ClusterInstance.serverless_v2(
                "Writer",
                auto_minor_version_upgrade=True,
            ),
            readers=[
                rds.ClusterInstance.serverless_v2(
                    "Reader",
                    auto_minor_version_upgrade=True,
                    scale_with_writer=True,
                ),
            ],
            vpc=self.vpc,
            vpc_subnets=ec2.SubnetSelection(subnet_type=ec2.SubnetType.PRIVATE_WITH_EGRESS),
            subnet_group=subnet_group,
            security_groups=[aurora_sg],
            default_database_name="gco_vectors",
            backup=rds.BackupProps(
                retention=Duration.days(aurora_config.get("backup_retention_days", 7)),
            ),
            deletion_protection=aurora_config.get("deletion_protection", False),
            removal_policy=RemovalPolicy.DESTROY,
            storage_encrypted=True,
            iam_authentication=True,
            cloudwatch_logs_exports=["postgresql"],
            monitoring_interval=Duration.seconds(60),
            cluster_identifier=f"{project_name}-pgvector-{self.deployment_region}",
        )

        # Construct-level cdk-nag suppressions for Aurora pgvector
        from gco.stacks.nag_suppressions import NagSuppression, acknowledge_nag_findings

        acknowledge_nag_findings(
            self.aurora_cluster,
            [
                NagSuppression(
                    id="AwsSolutions-RDS10",
                    reason=(
                        "Deletion protection is intentionally disabled for dev/test deployments. "
                        "Production deployments should set aurora_pgvector.deletion_protection=true "
                        "in cdk.json."
                    ),
                ),
                NagSuppression(
                    id="AwsSolutions-SMG4",
                    reason=(
                        "Aurora manages credential rotation via the RDS integration with Secrets "
                        "Manager. Manual Secrets Manager rotation is not required. "
                        "See: https://docs.aws.amazon.com/AmazonRDS/latest/AuroraUserGuide/rds-secrets-manager.html"
                    ),
                ),
                NagSuppression(
                    id="HIPAA.Security-RDSInstanceDeletionProtectionEnabled",
                    reason=(
                        "Deletion protection is intentionally disabled for dev/test deployments. "
                        "Production deployments should set aurora_pgvector.deletion_protection=true "
                        "in cdk.json."
                    ),
                ),
                NagSuppression(
                    id="NIST.800.53.R5-RDSInstanceDeletionProtectionEnabled",
                    reason=(
                        "Deletion protection is intentionally disabled for dev/test deployments. "
                        "Production deployments should set aurora_pgvector.deletion_protection=true "
                        "in cdk.json."
                    ),
                ),
                NagSuppression(
                    id="PCI.DSS.321-SecretsManagerUsingKMSKey",
                    reason=(
                        "Aurora Serverless v2 credentials in Secrets Manager are encrypted with "
                        "AWS-managed keys by default. Customer-managed KMS can be enabled if "
                        "required for PCI compliance."
                    ),
                ),
            ],
        )

        # Outputs
        CfnOutput(
            self,
            "AuroraPgvectorEndpoint",
            value=self.aurora_cluster.cluster_endpoint.hostname,
            description="Aurora pgvector cluster writer endpoint",
        )
        CfnOutput(
            self,
            "AuroraPgvectorReaderEndpoint",
            value=self.aurora_cluster.cluster_read_endpoint.hostname,
            description="Aurora pgvector cluster reader endpoint",
        )
        CfnOutput(
            self,
            "AuroraPgvectorPort",
            value=str(self.aurora_cluster.cluster_endpoint.port),
            description="Aurora pgvector cluster port",
        )
        CfnOutput(
            self,
            "AuroraPgvectorSecretArn",
            value=self.aurora_cluster.secret.secret_arn if self.aurora_cluster.secret else "",
            description="Aurora pgvector credentials secret ARN",
        )

        # Store endpoint in SSM for discovery by pods and external tools
        ssm.StringParameter(
            self,
            "AuroraPgvectorEndpointParam",
            parameter_name=f"/{project_name}/aurora-pgvector-endpoint-{self.deployment_region}",
            string_value=self.aurora_cluster.cluster_endpoint.hostname,
            description=f"Aurora pgvector endpoint for {self.deployment_region}",
        )

        # Grant the ServiceAccountRole read access to the Aurora secret
        # so pods can retrieve credentials via the ConfigMap + Secrets Manager.
        if self.aurora_cluster.secret:
            self.aurora_cluster.secret.grant_read(self.service_account_role)

    def _create_fsx_csi_driver_addon(self) -> None:
        """Create FSx CSI Driver add-on for Kubernetes integration.

        The FSx CSI driver enables Kubernetes pods to mount FSx for Lustre
        file systems as persistent volumes.
        """
        # Create IAM role for FSx CSI Driver using IRSA + Pod Identity
        self.fsx_csi_role = GCORegionalStack._create_irsa_role(
            self,
            "FsxCsiDriverRole",
            oidc_provider_arn=self.oidc_provider.open_id_connect_provider_arn,
            oidc_issuer_url=self.cluster.cluster_open_id_connect_issuer_url,
            service_account_names=["fsx-csi-controller-sa"],
            namespaces=["kube-system"],
        )

        # Add FSx CSI driver permissions
        self.fsx_csi_role.add_to_policy(
            iam.PolicyStatement(
                effect=iam.Effect.ALLOW,
                actions=[
                    "fsx:DescribeFileSystems",
                    "fsx:DescribeVolumes",
                    "fsx:CreateVolume",
                    "fsx:DeleteVolume",
                    "fsx:TagResource",
                ],
                resources=["*"],
            )
        )

        self.fsx_csi_role.add_to_policy(
            iam.PolicyStatement(
                effect=iam.Effect.ALLOW,
                actions=[
                    "ec2:DescribeInstances",
                    "ec2:DescribeVolumes",
                    "ec2:DescribeVpcs",
                    "ec2:DescribeSubnets",
                    "ec2:DescribeSecurityGroups",
                ],
                resources=["*"],
            )
        )

        # cdk-nag suppression: the FSx CSI driver role grants
        # ec2:Describe* APIs that don't support resource-level scoping.
        from gco.stacks.nag_suppressions import acknowledge_nag_findings

        acknowledge_nag_findings(
            self.fsx_csi_role,
            [
                {
                    "id": "AwsSolutions-IAM5",
                    "reason": (
                        "The FSx CSI driver role grants ec2:Describe* for volume "
                        "and network discovery. These AWS APIs do not support "
                        "resource-level IAM scoping — Resource: * is the only "
                        "valid form."
                    ),
                    "appliesTo": ["Resource::*"],
                },
            ],
        )

        # Create FSx CSI Driver add-on
        fsx_addon = eks.Addon(
            self,
            "FsxCsiDriverAddon",
            cluster=self.cluster,  # type: ignore[arg-type]
            addon_name="aws-fsx-csi-driver",
            addon_version=EKS_ADDON_FSX_CSI_DRIVER,
            preserve_on_delete=False,
            configuration_values={
                "node": {
                    "tolerations": self._ADDON_NODE_TOLERATIONS,
                },
                "controller": {
                    "tolerations": self._ADDON_NODE_TOLERATIONS,
                },
            },
        )

        # Append the PassRole statement for the FSx CSI role to the shared
        # AwsCustomResource execution role. See
        # _create_aws_custom_resource_role for the full rationale.
        self.aws_custom_resource_role.add_to_policy(
            iam.PolicyStatement(
                effect=iam.Effect.ALLOW,
                actions=["iam:PassRole"],
                resources=[self.fsx_csi_role.role_arn],
            )
        )

        # Update the add-on to use the IRSA role
        update_fsx_addon = cr.AwsCustomResource(
            self,
            "UpdateFsxCsiAddonRole",
            on_create=cr.AwsSdkCall(
                service="EKS",
                action="updateAddon",
                parameters={
                    "clusterName": self.cluster.cluster_name,
                    "addonName": "aws-fsx-csi-driver",
                    "serviceAccountRoleArn": self.fsx_csi_role.role_arn,
                },
                physical_resource_id=cr.PhysicalResourceId.of(
                    f"{self.cluster.cluster_name}-fsx-csi-role-update"
                ),
            ),
            on_update=cr.AwsSdkCall(
                service="EKS",
                action="updateAddon",
                parameters={
                    "clusterName": self.cluster.cluster_name,
                    "addonName": "aws-fsx-csi-driver",
                    "serviceAccountRoleArn": self.fsx_csi_role.role_arn,
                },
            ),
            role=self.aws_custom_resource_role,
        )

        update_fsx_addon.node.add_dependency(fsx_addon)
        update_fsx_addon.node.add_dependency(self.fsx_csi_role)
        update_fsx_addon.node.add_dependency(self.aws_custom_resource_role)

        # Expose the update-addon resource so _apply_kubernetes_manifests can
        # make the kubectl Lambda wait for the IRSA annotation patch to land
        # before it rollout-restarts the fsx-csi-controller. See the EFS CSI
        # equivalent for the full rationale — same race, same fix, same
        # symptom (PVCs stuck Pending with "no EC2 IMDS role found").
        self._fsx_csi_addon_role_update = update_fsx_addon

        # Create Pod Identity Association for FSx CSI driver
        eks_l1.CfnPodIdentityAssociation(
            self,
            "PodIdentity-fsx-csi",
            cluster_name=self.cluster.cluster_name,
            namespace="kube-system",
            service_account="fsx-csi-controller-sa",
            role_arn=self.fsx_csi_role.role_arn,
        )

    def _create_drift_detection(self) -> None:
        """Create CloudFormation drift detection on a daily schedule.

        Creates:
        - SNS topic (KMS-encrypted) for drift alerts
        - Lambda function that initiates drift detection on this stack, polls
          until detection completes, and publishes to SNS if drift is found
        - EventBridge rule on a daily schedule (configurable via cdk.json
          ``drift_detection.schedule_hours``) that invokes the Lambda

        Operators can disable drift detection entirely by setting
        ``drift_detection.enabled`` to ``false`` in cdk.json. When disabled,
        no resources are created.
        """
        drift_config = self.node.try_get_context("drift_detection") or {}
        if not drift_config.get("enabled", True):
            return

        schedule_hours = int(drift_config.get("schedule_hours", 24))

        # KMS key for SNS topic encryption. SNS with AWS-managed keys doesn't
        # allow CloudFormation/Lambda to publish, so we use a customer-managed
        # key we can grant publish access on.
        drift_topic_key = kms.Key(
            self,
            "DriftDetectionTopicKey",
            description="KMS key for GCO drift detection SNS topic",
            enable_key_rotation=True,
            removal_policy=RemovalPolicy.DESTROY,
        )

        self.drift_detection_topic = sns.Topic(
            self,
            "DriftDetectionTopic",
            display_name="GCO CloudFormation Drift Alerts",
            master_key=drift_topic_key,
        )

        # IAM role for the drift detection Lambda
        drift_lambda_role = iam.Role(
            self,
            "DriftDetectionLambdaRole",
            assumed_by=iam.ServicePrincipal("lambda.amazonaws.com"),
            managed_policies=[
                iam.ManagedPolicy.from_aws_managed_policy_name(
                    "service-role/AWSLambdaBasicExecutionRole"
                ),
            ],
        )

        # CloudFormation drift APIs operate at the stack level; the API does
        # not support resource-level ARN scoping for these actions, so we scope
        # to this stack's ARN where supported and accept "*" where not.
        drift_lambda_role.add_to_policy(
            iam.PolicyStatement(
                effect=iam.Effect.ALLOW,
                actions=[
                    "cloudformation:DetectStackDrift",
                    "cloudformation:DescribeStackDriftDetectionStatus",
                    "cloudformation:DescribeStackResourceDrifts",
                    "cloudformation:DescribeStackResource",
                    "cloudformation:DescribeStackResources",
                ],
                resources=["*"],
            )
        )

        self.drift_detection_topic.grant_publish(drift_lambda_role)

        # Lambda function — one per stack; stack name is baked into env vars
        drift_lambda = lambda_.Function(
            self,
            "DriftDetectionFunction",
            runtime=getattr(lambda_.Runtime, LAMBDA_PYTHON_RUNTIME),
            handler="handler.lambda_handler",
            code=lambda_.Code.from_asset("lambda/drift-detection"),
            timeout=Duration.minutes(14),  # Leave headroom under Lambda 15-min cap
            memory_size=256,
            role=drift_lambda_role,
            environment={
                "STACK_NAME": self.stack_name,
                "SNS_TOPIC_ARN": self.drift_detection_topic.topic_arn,
                "REGION": self.deployment_region,
            },
            tracing=lambda_.Tracing.ACTIVE,
        )

        # Dead-letter queue for EventBridge → Lambda target failures.
        # Captures events that fail to reach the Lambda (e.g. due to
        # throttling or permission issues) so operators can retry or
        # investigate. Required by Serverless-EventBusDLQ cdk-nag rule.
        drift_rule_dlq = sqs.Queue(
            self,
            "DriftDetectionRuleDlq",
            retention_period=Duration.days(14),
            enforce_ssl=True,
            encryption=sqs.QueueEncryption.SQS_MANAGED,
            removal_policy=RemovalPolicy.DESTROY,
        )

        # DLQs themselves are terminal — they don't need their own DLQ.
        # Suppress the circular AwsSolutions-SQS3 nag finding.
        from gco.stacks.nag_suppressions import acknowledge_nag_findings

        acknowledge_nag_findings(
            drift_rule_dlq,
            [
                {
                    "id": "AwsSolutions-SQS3",
                    "reason": (
                        "This queue IS the dead-letter queue for the "
                        "DriftDetectionSchedule EventBridge rule. A DLQ for a "
                        "DLQ is circular; if events fail to reach this queue "
                        "they are captured by EventBridge's own retry metrics "
                        "(CloudWatch FailedInvocations)."
                    ),
                },
            ],
        )

        # EventBridge rule — daily schedule by default
        events.Rule(
            self,
            "DriftDetectionSchedule",
            description=(f"Daily CloudFormation drift detection for {self.stack_name}"),
            schedule=events.Schedule.rate(Duration.hours(schedule_hours)),
            targets=[
                events_targets.LambdaFunction(
                    drift_lambda,
                    dead_letter_queue=drift_rule_dlq,
                    retry_attempts=2,
                )
            ],
        )

        # Outputs for operators to subscribe to the topic
        CfnOutput(
            self,
            "DriftDetectionTopicArn",
            value=self.drift_detection_topic.topic_arn,
            description=(
                f"SNS topic ARN for CloudFormation drift alerts in "
                f"{self.deployment_region}. Subscribe an endpoint (email, "
                f"Slack, PagerDuty) to receive drift notifications."
            ),
        )

        # cdk-nag suppressions for this component
        from gco.stacks.nag_suppressions import acknowledge_nag_findings

        acknowledge_nag_findings(
            drift_lambda_role,
            [
                {
                    "id": "AwsSolutions-IAM4",
                    "reason": (
                        "AWSLambdaBasicExecutionRole provides standard "
                        "CloudWatch Logs permissions required for Lambda "
                        "logging. This is the AWS-recommended managed policy."
                    ),
                },
                {
                    "id": "AwsSolutions-IAM5",
                    "reason": (
                        "CloudFormation drift detection APIs (DetectStackDrift, "
                        "DescribeStackDriftDetectionStatus, "
                        "DescribeStackResourceDrifts) cannot be scoped to a "
                        "specific stack resource via IAM; the action-level "
                        "scoping requires wildcard resources. The Lambda's "
                        "environment pins it to a single stack name, so the "
                        "effective blast radius is limited. The "
                        "``kms:GenerateDataKey*`` action wildcard is the "
                        "AWS-recommended grant for publishing to the "
                        "KMS-encrypted drift-detection SNS topic."
                    ),
                    "appliesTo": [
                        "Resource::*",
                        "Action::kms:GenerateDataKey*",
                    ],
                },
            ],
        )

    def _create_mcp_role(self) -> None:
        """Create dedicated IAM role for the MCP server.

        The MCP server exposes GCO CLI tools to LLM agents. Without a dedicated
        role, the server would inherit the full ambient credentials of the user
        who launches it (often an administrator). This method creates a
        least-privilege role that the MCP server can assume at startup via
        ``GCO_MCP_ROLE_ARN``.

        Permissions are scoped to the minimum needed by the tools exposed:

        - ``eks:DescribeCluster`` on this regional EKS cluster ARN only.
        - ``s3:GetObject`` on model weights buckets. The model bucket lives in
          the global stack, so we scope to the same name pattern used by the
          service account role (``{project_name}-*``). This is a deliberate
          compromise: a precise cross-stack ARN export would force a tight
          dependency on the global stack, and cdk-nag will flag it anyway
          because the bucket name is auto-generated.
        - ``cloudwatch:GetMetricData`` / ``cloudwatch:ListMetrics``. These APIs
          do not support resource-level IAM, so wildcard is required. Read-only.
        - ``sqs:SendMessage`` scoped to this region's job queue ARN only.

        The trust policy uses ``AccountRootPrincipal`` so any IAM user/role in
        the account can assume it (gated by an explicit sts:AssumeRole
        permission on the caller — standard AWS behavior). Operators who want
        to restrict assumption further should add an external-id or principal
        condition to the trust policy after deployment.

        Operators can disable this component entirely by setting
        ``mcp_server.enabled`` to ``false`` in cdk.json.
        """
        mcp_config = self.node.try_get_context("mcp_server") or {}
        if not mcp_config.get("enabled", True):
            return

        project_name = self.config.get_project_name()

        self.mcp_server_role = iam.Role(
            self,
            "McpServerRole",
            assumed_by=iam.AccountRootPrincipal(),
            description=(
                "Least-privilege role assumed by the GCO MCP server at startup. "
                "Grants only the permissions needed by MCP tools: eks:DescribeCluster, "
                "s3:GetObject on model buckets, cloudwatch read-only metrics, and "
                "sqs:SendMessage to the regional job queue."
            ),
            max_session_duration=Duration.hours(12),
        )

        # eks:DescribeCluster on this region's cluster only
        self.mcp_server_role.add_to_policy(
            iam.PolicyStatement(
                effect=iam.Effect.ALLOW,
                actions=["eks:DescribeCluster"],
                resources=[self.cluster.cluster_arn],
            )
        )

        # s3:GetObject on model weights buckets. Bucket name is auto-generated
        # in the global stack, so we match the same prefix pattern used by the
        # service account role.
        self.mcp_server_role.add_to_policy(
            iam.PolicyStatement(
                effect=iam.Effect.ALLOW,
                actions=["s3:GetObject", "s3:ListBucket"],
                resources=[
                    f"arn:aws:s3:::{project_name}-*",
                    f"arn:aws:s3:::{project_name}-*/*",
                ],
            )
        )

        # CloudWatch read-only metrics APIs. These APIs do not support
        # resource-level IAM so wildcard is required.
        self.mcp_server_role.add_to_policy(
            iam.PolicyStatement(
                effect=iam.Effect.ALLOW,
                actions=[
                    "cloudwatch:GetMetricData",
                    "cloudwatch:GetMetricStatistics",
                    "cloudwatch:ListMetrics",
                ],
                resources=["*"],
            )
        )

        # sqs:SendMessage scoped to the regional job queue only
        self.mcp_server_role.add_to_policy(
            iam.PolicyStatement(
                effect=iam.Effect.ALLOW,
                actions=["sqs:SendMessage", "sqs:GetQueueUrl", "sqs:GetQueueAttributes"],
                resources=[self.job_queue.queue_arn],
            )
        )

        # Export the role ARN so operators can set GCO_MCP_ROLE_ARN in their
        # MCP server environment.
        CfnOutput(
            self,
            "McpServerRoleArn",
            value=self.mcp_server_role.role_arn,
            description=(
                "IAM role ARN for the GCO MCP server. Set GCO_MCP_ROLE_ARN to "
                "this value when launching the MCP server so it assumes a "
                "least-privilege role instead of ambient credentials."
            ),
            export_name=f"{project_name}-mcp-server-role-arn-{self.deployment_region}",
        )

        # cdk-nag suppressions: CloudWatch metrics APIs cannot be scoped.
        from gco.stacks.nag_suppressions import acknowledge_nag_findings

        acknowledge_nag_findings(
            self.mcp_server_role,
            [
                {
                    "id": "AwsSolutions-IAM5",
                    "reason": (
                        "The CloudWatch metrics APIs (GetMetricData, "
                        "GetMetricStatistics, ListMetrics) do not support "
                        "resource-level IAM; wildcard resource is required. "
                        "The S3 permissions use the {project_name}-* prefix "
                        "pattern because the model weights bucket name is "
                        "auto-generated by CDK in the global stack and a "
                        "cross-stack ARN export would create tight stack "
                        "coupling. All actions are read-only or scoped "
                        "send-only (SQS)."
                    ),
                    "appliesTo": [
                        "Resource::*",
                    ],
                },
            ],
        )

    def _create_outputs(self) -> None:
        """Create CloudFormation outputs for cluster information"""
        project_name = self.config.get_project_name()

        # Export cluster information
        CfnOutput(
            self,
            "ClusterName",
            value=self.cluster.cluster_name,
            description=f"EKS cluster name for {self.deployment_region}",
            export_name=f"{project_name}-cluster-name-{self.deployment_region}",
        )

        CfnOutput(
            self,
            "ClusterArn",
            value=self.cluster.cluster_arn,
            description=f"EKS cluster ARN for {self.deployment_region}",
            export_name=f"{project_name}-cluster-arn-{self.deployment_region}",
        )

        CfnOutput(
            self,
            "ClusterEndpoint",
            value=self.cluster.cluster_endpoint,
            description=f"EKS cluster endpoint for {self.deployment_region}",
            export_name=f"{project_name}-cluster-endpoint-{self.deployment_region}",
        )

        CfnOutput(
            self,
            "ClusterSecurityGroupId",
            value=self.cluster.cluster_security_group_id,
            description=f"EKS cluster security group ID for {self.deployment_region}",
            export_name=f"{project_name}-cluster-sg-{self.deployment_region}",
        )

        CfnOutput(
            self,
            "VpcId",
            value=self.vpc.vpc_id,
            description=f"VPC ID for {self.deployment_region}",
            export_name=f"{project_name}-vpc-id-{self.deployment_region}",
        )

        # Export public subnet IDs for ALB
        public_subnet_ids = [subnet.subnet_id for subnet in self.vpc.public_subnets]
        CfnOutput(
            self,
            "PublicSubnetIds",
            value=Fn.join(",", public_subnet_ids),
            description=f"Public subnet IDs for ALB in {self.deployment_region}",
            export_name=f"{project_name}-public-subnets-{self.deployment_region}",
        )

        # Note: ALB is created by AWS Load Balancer Controller via Ingress
        # The ALB ARN is registered with Global Accelerator by the GA registration Lambda

    def get_cluster(self) -> eks.Cluster:
        """Get the EKS cluster"""
        return self.cluster

    def get_vpc(self) -> ec2.Vpc:
        """Get the VPC"""
        return self.vpc
