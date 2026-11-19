"""Analytics stack for GCO - optional ML/analytics environment.

Instantiated only when ``analytics_environment.enabled=true`` in ``cdk.json``.
When the toggle is ``false`` (the default), ``app.py`` skips creating it so
``cdk synth`` emits no SageMaker, EMR Serverless, or Cognito resources.

Resources (wired in this order):

1. ``_create_kms_key``                                    — ``Analytics_KMS_Key``
2. ``_create_vpc_and_endpoints``                          — private VPC + endpoints
3. ``_create_access_logs_bucket``                         — S3 access-logs bucket
4. ``_create_studio_only_bucket``                         — ``Studio_Only_Bucket``
5. ``_create_studio_efs``                                 — ``Studio_EFS``
6. ``_create_execution_role_and_grants``                  — ``SageMaker_Execution_Role``
7. ``_grant_sagemaker_role_on_cluster_shared_bucket``     — cross-region IAM grant
8. ``_create_studio_domain``                              — ``sagemaker.CfnDomain``
9. ``_create_emr_app``                                    — ``emrserverless.CfnApplication``
10. ``_create_cognito_pool``                              — Cognito pool + client + domain
11. ``_create_presigned_url_lambda``                      — ``Presigned_URL_Lambda``
12. ``_apply_nag_suppressions``                           — analytics-branch nag dispatch

The API Gateway ``/studio/*`` wiring that consumes this Lambda lives in
``gco/stacks/api_gateway_global_stack.py``.
"""

from __future__ import annotations

from typing import Any, cast

from aws_cdk import (
    CfnElement,
    CfnOutput,
    Duration,
    RemovalPolicy,
    Stack,
    Tags,
)
from aws_cdk import aws_cognito as cognito
from aws_cdk import aws_ec2 as ec2
from aws_cdk import aws_efs as efs
from aws_cdk import aws_emrserverless as emrserverless
from aws_cdk import aws_iam as iam
from aws_cdk import aws_kms as kms
from aws_cdk import aws_lambda as lambda_
from aws_cdk import aws_logs as logs
from aws_cdk import aws_s3 as s3
from aws_cdk import aws_sagemaker as sagemaker
from aws_cdk import custom_resources as cr
from constructs import Construct

from gco.config.config_loader import ConfigLoader
from gco.stacks.constants import (
    EMR_SERVERLESS_RELEASE_LABEL,
    LAMBDA_PYTHON_RUNTIME,
    SAGEMAKER_ROLE_NAME_PREFIX,
    cluster_shared_ssm_parameter_prefix,
    cognito_domain_prefix_default,
    s3_bucket_kms_conditions,
)
from gco.stacks.lambda_assets import lambda_source_code
from gco.stacks.nag_suppressions import apply_all_suppressions

# <pyflowchart-code-diagram> BEGIN - auto-inserted, do not edit
# Generated at (UTC): 2026-11-19T18:18:19Z
# Generated from Git commit: ae5e8fb039b08d74dd7da8ba37d00fd590f05474
# Flowchart(s) generated from this file:
#   * ``GCOAnalyticsStack.__init__`` -> ``diagrams/code_diagrams/gco/stacks/analytics_stack.GCOAnalyticsStack___init__.html``
#     (PNG: ``diagrams/code_diagrams/gco/stacks/analytics_stack.GCOAnalyticsStack___init__.png``)
#   * ``GCOAnalyticsStack._create_execution_role_and_grants`` -> ``diagrams/code_diagrams/gco/stacks/analytics_stack.GCOAnalyticsStack__create_execution_role_and_grants.html``
#     (PNG: ``diagrams/code_diagrams/gco/stacks/analytics_stack.GCOAnalyticsStack__create_execution_role_and_grants.png``)
#   * ``GCOAnalyticsStack._create_studio_domain`` -> ``diagrams/code_diagrams/gco/stacks/analytics_stack.GCOAnalyticsStack__create_studio_domain.html``
#     (PNG: ``diagrams/code_diagrams/gco/stacks/analytics_stack.GCOAnalyticsStack__create_studio_domain.png``)
# Regenerate with ``SOURCE_DATE_EPOCH=<unix-seconds> GCO_DIAGRAM_SOURCE_COMMIT=<40-char-sha> python diagrams/generate.py --code-only``.
# <pyflowchart-code-diagram> END


#: Handler, timeout and memory of the presigned-URL function behind
#: ``/studio/login`` (``_create_presigned_url_lambda``). Module constants so the
#: stand-in that ``scripts/generate_api_gateway_openapi.py`` synthesizes in its
#: place reports what this stack deploys; its runtime is ``LAMBDA_PYTHON_RUNTIME``.
PRESIGNED_URL_HANDLER = "handler.lambda_handler"
#: API Gateway's maximum integration timeout, so the function times out first.
PRESIGNED_URL_TIMEOUT_SECONDS = 29
PRESIGNED_URL_MEMORY_MB = 256


def _parse_removal(value: str) -> RemovalPolicy:
    """Map a cdk.json removal-policy string to ``aws_cdk.RemovalPolicy``.

    Translates ``analytics_environment.{efs,cognito}.removal_policy`` into
    the matching enum member. Accepts ``"retain"`` / ``"destroy"``
    (case-insensitive); raises ``ValueError`` on anything else.
    """
    normalized = value.strip().lower()
    if normalized == "retain":
        return RemovalPolicy.RETAIN
    if normalized == "destroy":
        return RemovalPolicy.DESTROY
    raise ValueError(
        f"analytics_environment removal_policy must be 'retain' or 'destroy', got {value!r}"
    )


class GCOAnalyticsStack(Stack):
    """Optional ML/analytics environment: SageMaker Studio, EMR Serverless, Cognito.

    Only instantiated when ``analytics_environment.enabled=true``. Lives in
    the API gateway region so the presigned-URL Lambda can wire into the
    existing ``/studio/*`` routes on ``GCOApiGatewayGlobalStack`` without
    a cross-region hop.
    """

    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        config: ConfigLoader,
        api_gateway_secret_arn: str | None = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)

        self.config = config
        self.project_name = config.get_project_name()
        # ``api_gateway_secret_arn`` is reserved for future auth wiring;
        # accepted now so the constructor signature is stable.
        self.api_gateway_secret_arn = api_gateway_secret_arn

        cfg = config.get_analytics_config()
        self.hyperpod_enabled: bool = bool(cfg["hyperpod"]["enabled"])
        self.canvas_enabled: bool = bool(cfg["canvas"]["enabled"])
        self.efs_removal: RemovalPolicy = _parse_removal(cfg["efs"]["removal_policy"])
        self.cognito_removal: RemovalPolicy = _parse_removal(cfg["cognito"]["removal_policy"])
        self._cognito_domain_prefix_override: str | None = cfg["cognito"].get("domain_prefix")

        # Wiring order is load-bearing — each helper consumes resources from
        # earlier helpers (EFS ARN → execution role → studio domain, etc.).
        self._create_kms_key()
        self._create_vpc_and_endpoints()
        self._create_access_logs_bucket()
        self._create_studio_only_bucket()
        self._create_studio_efs()
        self._create_execution_role_and_grants()
        self._grant_sagemaker_role_on_cluster_shared_bucket()
        self._create_studio_domain()
        self._create_emr_app()
        self._create_cognito_pool()
        self._create_presigned_url_lambda()
        self._apply_nag_suppressions()

    # ==================================================================
    # KMS + VPC
    # ==================================================================

    def _create_kms_key(self) -> None:
        """Create ``Analytics_KMS_Key`` with rotation + 7-day pending window.

        Customer-managed so every analytics-owned bucket, the Studio EFS,
        and SageMaker-written artifacts share a single encryption boundary.
        ``removal_policy=DESTROY`` follows the iteration-loop posture
        — the 7-day pending window gives recovery headroom without retaining
        the key past a ``cdk destroy gco-analytics`` cycle.
        """
        self.kms_key = kms.Key(
            self,
            "AnalyticsKmsKey",
            description="Analytics_KMS_Key - encrypts analytics S3 buckets, Studio EFS, SageMaker artifacts",
            enable_key_rotation=True,
            pending_window=Duration.days(7),
            removal_policy=RemovalPolicy.DESTROY,
        )

        # Grant encrypt/decrypt to service principals that need to operate
        # on analytics-owned resources encrypted by this key.
        service_principals = [
            ("logs.amazonaws.com", self.region),
            ("sagemaker.amazonaws.com", self.region),
            ("s3.amazonaws.com", self.region),
            ("elasticfilesystem.amazonaws.com", self.region),
        ]
        for principal, region in service_principals:
            self.kms_key.add_to_resource_policy(
                iam.PolicyStatement(
                    sid=f"Allow{principal.split('.')[0].capitalize()}Encrypt",
                    effect=iam.Effect.ALLOW,
                    principals=[iam.ServicePrincipal(principal, region=region)],
                    actions=[
                        "kms:Encrypt",
                        "kms:Decrypt",
                        "kms:ReEncrypt*",
                        "kms:GenerateDataKey*",
                        "kms:DescribeKey",
                    ],
                    resources=["*"],  # key-policy scope — always the key itself
                )
            )

    def _create_vpc_and_endpoints(self) -> None:
        """Create the analytics VPC plus every VPC endpoint Studio needs.

        Two subnet tiers in two AZs: private subnets (with egress) for the
        Studio domain, EMR Serverless, the EFS mount targets and the
        endpoints, and small public subnets whose only resource is the one NAT
        gateway. The NAT gateway gives notebooks internet egress (pip install,
        git clone, HuggingFace, PyPI); notebooks themselves never land on the
        public subnets. The nine interface endpoints plus the S3 gateway
        endpoint keep AWS API traffic from Studio, EMR and EFS off the NAT
        gateway. No flow log is attached (see ``add_analytics_vpc_suppressions``).
        """
        self.vpc = ec2.Vpc(
            self,
            "AnalyticsVpc",
            max_azs=2,
            nat_gateways=1,
            subnet_configuration=[
                ec2.SubnetConfiguration(
                    name="AnalyticsPrivate",
                    subnet_type=ec2.SubnetType.PRIVATE_WITH_EGRESS,
                    cidr_mask=24,
                ),
                ec2.SubnetConfiguration(
                    name="AnalyticsPublic",
                    subnet_type=ec2.SubnetType.PUBLIC,
                    cidr_mask=28,
                ),
            ],
        )

        # Gateway endpoint for S3 — route tables are wired up automatically.
        self.vpc.add_gateway_endpoint(
            "S3GatewayEndpoint",
            service=ec2.GatewayVpcEndpointAwsService.S3,
        )

        # Interface endpoints — one per AWS service required by Studio. Each
        # lands in the VPC's private subnets using the default
        # VPC-endpoint security group.
        interface_services: dict[str, ec2.InterfaceVpcEndpointAwsService] = {
            "SagemakerApiEndpoint": ec2.InterfaceVpcEndpointAwsService.SAGEMAKER_API,
            "SagemakerRuntimeEndpoint": ec2.InterfaceVpcEndpointAwsService.SAGEMAKER_RUNTIME,
            "SagemakerStudioEndpoint": ec2.InterfaceVpcEndpointAwsService.SAGEMAKER_STUDIO,
            "SagemakerNotebookEndpoint": ec2.InterfaceVpcEndpointAwsService.SAGEMAKER_NOTEBOOK,
            "StsEndpoint": ec2.InterfaceVpcEndpointAwsService.STS,
            "CloudWatchLogsEndpoint": ec2.InterfaceVpcEndpointAwsService.CLOUDWATCH_LOGS,
            "EcrEndpoint": ec2.InterfaceVpcEndpointAwsService.ECR,
            "EcrDockerEndpoint": ec2.InterfaceVpcEndpointAwsService.ECR_DOCKER,
            "EfsEndpoint": ec2.InterfaceVpcEndpointAwsService.ELASTIC_FILESYSTEM,
        }
        for construct_id, service in interface_services.items():
            self.vpc.add_interface_endpoint(
                construct_id,
                service=service,
                subnets=ec2.SubnetSelection(subnet_type=ec2.SubnetType.PRIVATE_WITH_EGRESS),
            )

        # Each interface endpoint's default security group allows 443 from the
        # VPC CIDR (an ``Fn::GetAtt`` token cdk-nag can't resolve), so the
        # SG-ingress rules throw. Scope the acknowledgment to the VPC construct
        # so it covers every endpoint SG under it without touching the stack.
        from gco.stacks.nag_suppressions import acknowledge_security_group_cidr_findings

        acknowledge_security_group_cidr_findings(
            self.vpc,
            reason=(
                "The Studio VPC interface endpoints use their default security "
                "group, which allows HTTPS (443) ingress from the VPC CIDR "
                "only, referenced via an ``Fn::GetAtt`` token that cdk-nag "
                "cannot resolve at synth time. Ingress is restricted to "
                "intra-VPC traffic — the tightest source for private "
                "endpoint access."
            ),
        )

    # ==================================================================
    # S3 buckets
    # ==================================================================

    def _create_access_logs_bucket(self) -> None:
        """Create the dedicated access-logs bucket for ``Studio_Only_Bucket``.

        Server-side encryption uses S3-managed keys (SSE-S3): S3 server access
        logging requires an SSE-S3 destination bucket (with SSE-KMS, log
        objects might be delivered encrypted with a key the owner cannot
        use), while the bucket it logs is KMS-encrypted. The SSE-KMS
        default-encryption nag rules are acknowledged on this bucket only.
        """
        from gco.stacks.nag_suppressions import acknowledge_access_log_destination_encryption

        self.access_logs_bucket = s3.Bucket(
            self,
            "AnalyticsAccessLogsBucket",
            encryption=s3.BucketEncryption.S3_MANAGED,
            block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
            enforce_ssl=True,
            versioned=True,
            removal_policy=RemovalPolicy.DESTROY,
            auto_delete_objects=True,
            lifecycle_rules=[
                s3.LifecycleRule(
                    id="ExpireAccessLogs",
                    enabled=True,
                    expiration=Duration.days(90),
                )
            ],
        )
        acknowledge_access_log_destination_encryption(self.access_logs_bucket)

    def _create_studio_only_bucket(self) -> None:
        """Create ``Studio_Only_Bucket`` for notebook-private scratch + outputs.

        The physical name is CloudFormation-generated
        (``<stack>-studioonlybucket…``): S3 bucket names are a global namespace
        and a deleted name is not reliably reusable, so a fixed
        project/account/region name would make every destroy-and-redeploy a
        collision hazard. Nothing needs to reconstruct the name — the only
        grant is the ``SageMaker_Execution_Role`` grant on the construct's own
        ARN, and the CLI resolves the bucket from this stack's resources. The
        job-pod isolation property (``tests/test_analytics_bucket_isolation_
        property.py``) classifies grants by construct token, so it does not
        depend on a name prefix either. KMS-encrypted with ``self.kms_key``;
        every access path goes through the ``SageMaker_Execution_Role`` grant
        — no other principal is granted access.
        """
        self.studio_only_bucket = s3.Bucket(
            self,
            "StudioOnlyBucket",
            encryption=s3.BucketEncryption.KMS,
            encryption_key=self.kms_key,
            bucket_key_enabled=True,
            block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
            enforce_ssl=True,
            versioned=True,
            removal_policy=RemovalPolicy.DESTROY,
            auto_delete_objects=True,
            server_access_logs_bucket=self.access_logs_bucket,
            server_access_logs_prefix="studio-only/",
        )

        # Belt-and-suspenders Deny for insecure transport, duplicating the
        # ``enforce_ssl=True`` semantics with a verifiable SID in the
        # synthesized template (mirrors the ``DenyInsecureTransport`` pattern
        # used by ``Cluster_Shared_Bucket`` in ``GCOGlobalStack``).
        self.studio_only_bucket.add_to_resource_policy(
            iam.PolicyStatement(
                sid="DenyInsecureTransport",
                effect=iam.Effect.DENY,
                principals=[iam.AnyPrincipal()],
                actions=["s3:*"],
                resources=[
                    self.studio_only_bucket.bucket_arn,
                    f"{self.studio_only_bucket.bucket_arn}/*",
                ],
                conditions={"Bool": {"aws:SecureTransport": "false"}},
            )
        )

    def _tag_retainable(self, construct: Construct) -> None:
        """Tag a resource ``removal_policy: retain`` can leave behind as this project's.

        ``gco analytics doctor`` reports a Studio EFS file system or Cognito
        user pool tagged ``gco:analytics:managed=true`` and
        ``gco:project=<project>`` as an orphan once no live analytics stack
        owns it (``cli/analytics_user_mgmt.scan_orphan_analytics_resources``),
        including after CloudFormation has forgotten the deleted stack. Both
        resources take tag changes in place.
        """
        Tags.of(construct).add("gco:analytics:managed", "true")
        Tags.of(construct).add("gco:project", self.project_name)

    # ==================================================================
    # Studio EFS
    # ==================================================================

    def _create_studio_efs(self) -> None:
        """Create ``Studio_EFS`` with KMS encryption + TLS in transit.

        Per-user access points are created lazily by the presigned-URL
        Lambda on first profile creation. No access points are defined
        here, so the file system's ``/`` root is effectively inaccessible
        until the Lambda materializes a per-user AP.

        The dedicated security group only allows the VPC's private
        CIDR on TCP/2049 (NFS). SageMaker Studio mount traffic originates
        from the Studio compute subnet, which shares the VPC with this EFS.
        """
        self.studio_efs_security_group = ec2.SecurityGroup(
            self,
            "StudioEfsSecurityGroup",
            vpc=self.vpc,
            description="SG for Studio_EFS - allows NFS from the analytics VPC only",
            allow_all_outbound=False,
        )
        self.studio_efs_security_group.add_ingress_rule(
            peer=ec2.Peer.ipv4(self.vpc.vpc_cidr_block),
            connection=ec2.Port.tcp(2049),
            description="NFS from analytics VPC private subnets",
        )

        # The EFS SG ingress allows NFS (2049) from the VPC CIDR (an
        # ``Fn::GetAtt`` token cdk-nag can't resolve), so the SG-ingress rules
        # throw. Scope the acknowledgment to the EFS SG construct itself.
        from gco.stacks.nag_suppressions import acknowledge_security_group_cidr_findings

        acknowledge_security_group_cidr_findings(
            self.studio_efs_security_group,
            reason=(
                "The Studio_EFS security group allows NFS (2049) ingress from "
                "the VPC CIDR only, referenced via an ``Fn::GetAtt`` token "
                "that cdk-nag cannot resolve at synth time. Ingress is "
                "restricted to intra-VPC traffic from the Studio compute "
                "subnet that mounts the file system."
            ),
        )

        self.studio_efs = efs.FileSystem(
            self,
            "StudioEfs",
            vpc=self.vpc,
            vpc_subnets=ec2.SubnetSelection(subnet_type=ec2.SubnetType.PRIVATE_WITH_EGRESS),
            encrypted=True,
            kms_key=self.kms_key,
            enable_automatic_backups=True,
            removal_policy=self.efs_removal,
            security_group=self.studio_efs_security_group,
        )
        self._tag_retainable(self.studio_efs)

    # ==================================================================
    # SageMaker execution role + grants
    # ==================================================================

    def _create_execution_role_and_grants(self) -> None:
        """Create ``SageMaker_Execution_Role`` and attach its (non-cluster-shared) grants.

        Role name begins with ``AmazonSageMaker`` — SageMaker
        requires this prefix for any role used by a Studio domain. Grants
        attached here:

        * RW on ``Studio_Only_Bucket`` + KMS on ``Analytics_KMS_Key``
        * ``execute-api:Invoke`` with every HTTP method on the GCO API's
          ``/prod/*/api/v1/*`` and ``/prod/*/inference/*`` routes in the API
          Gateway Region, so notebooks can submit and delete jobs and manage
          inference endpoints as well as read (any REST API id: this stack
          deploys before the API Gateway stack, whose API id it cannot know)
        * ``sqs:SendMessage`` on each workload Region's job queue
          (``<project>-jobs-<region>``; not the dead-letter queues)
        * ``ssm:GetParameter`` on the three ``Cluster_Shared_Bucket`` metadata
          parameters (``name``, ``arn``, ``region``) in the global region —
          lets notebooks look up the bucket at runtime without a per-user
          export step
        * EFS mount actions on ``Studio_EFS`` (specific AP arn is added by
          the presigned-URL Lambda at runtime; the role-level grant here is
          scoped to the EFS ARN)
        * HyperPod training-job actions when ``hyperpod.enabled=true``
        * AWS-managed ``AmazonSageMakerCanvasFullAccess`` when
          ``canvas.enabled=true`` (opt-in no-code ML app)
        * AWS-managed ``AmazonSageMakerFullAccess`` — always attached
          whenever analytics is enabled. Covers the full SageMaker
          control-plane surface including MLflow Apps
          (``CreateMlflowApp``/``ListMlflowApps``/``DescribeMlflowApp``),
          MLflow Tracking Servers, Model Registry, Studio space/app
          lifecycle, and adjacent services (S3, ECR, CloudWatch Logs,
          etc.) that SageMaker needs to launch training jobs, create
          apps, and render the Studio IDE. We pair the managed policy
          with an inline ``sagemaker-mlflow:*`` statement (next block)
          because the managed policy does not cover the
          ``sagemaker-mlflow`` data-plane namespace the MLflow SDK
          talks to. MLflow does not have its own sub-toggle — the
          managed policy replaces our previous enumerated
          ``sagemaker:*MlflowTrackingServer*`` inline grant.

        The ``Cluster_Shared_Bucket`` grant lives in its own helper
        (:meth:`_grant_sagemaker_role_on_cluster_shared_bucket`) because the
        bucket ARN is resolved via a cross-region SSM read.
        """
        self.sagemaker_execution_role = iam.Role(
            self,
            "SagemakerExecutionRole",
            role_name=f"{SAGEMAKER_ROLE_NAME_PREFIX}-{self.project_name}-analytics-exec-{self.region}",
            assumed_by=iam.ServicePrincipal("sagemaker.amazonaws.com"),
            description=(
                "SageMaker_Execution_Role - assumed by notebooks in the Studio "
                "domain. Grants RW on Studio_Only_Bucket and (via a separate "
                "cross-region policy) Cluster_Shared_Bucket, plus GCO API access "
                "(every method under /api/v1 and /inference), SQS job submission, "
                "and cross-region ssm:GetParameter on the Cluster_Shared_Bucket "
                "metadata parameters."
            ),
        )

        # Bucket + KMS grants — studio-only scratch space. Analytics_KMS_Key
        # already has encrypt/decrypt in its key policy for the sagemaker
        # service principal, but role-level grants are still required for
        # IAM-side authorization per the double-auth model.
        self.studio_only_bucket.grant_read_write(self.sagemaker_execution_role)
        self.kms_key.grant_encrypt_decrypt(self.sagemaker_execution_role)

        # SageMaker needs CreateGrant on the KMS key to delegate encryption
        # to EBS when creating space volumes. The grant is scoped to the
        # key and conditioned on the grantee being an AWS service.
        self.kms_key.grant(
            self.sagemaker_execution_role,
            "kms:CreateGrant",
            "kms:DescribeKey",
        )

        # GCO API scope — notebooks need both read-only GET operations
        # (list jobs, describe endpoints, fetch health) and job/inference
        # submission actions (POST manifests, PUT template updates, DELETE
        # jobs). Grant the full ``/api/v1/*`` method surface instead of
        # GET-only so users can submit new jobs, manage templates, and
        # tear things down from inside a notebook without bouncing
        # through a service account.
        #
        # The API id cannot be named: the API Gateway stack depends on this
        # stack (it wires this stack's presigned-URL Lambda into
        # /studio/login), so this stack deploys first and the API does not
        # exist yet. The grant takes any REST API id in the API Gateway
        # Region and account, under the prod stage and the two path trees.
        api_gw_region = self.config.get_api_gateway_region()
        self.sagemaker_execution_role.add_to_policy(
            iam.PolicyStatement(
                effect=iam.Effect.ALLOW,
                actions=["execute-api:Invoke"],
                resources=[
                    # ``*/prod/*/api/v1/*`` — any API id, any HTTP method
                    # (GET/POST/PUT/DELETE/PATCH), any path below
                    # /api/v1/.  /studio/* is explicitly excluded; Canvas
                    # users go through their own Cognito-authorized
                    # ``/studio/login`` route.
                    f"arn:{self.partition}:execute-api:{api_gw_region}:{self.account}:"
                    "*/prod/*/api/v1/*",
                    # ``/inference/*`` proxies through to regional ALBs
                    # for in-cluster model endpoints — notebooks need
                    # the full method surface here too.
                    f"arn:{self.partition}:execute-api:{api_gw_region}:{self.account}:"
                    "*/prod/*/inference/*",
                ],
            )
        )

        # SQS job submission — each workload Region's job queue, named
        # ``<project>-jobs-<region>`` by ``GCORegionalStack._create_sqs_queue``.
        # The queues live in the regional stacks, so the ARNs are built from
        # the names; the dead-letter queues (``<project>-jobs-dlq-<region>``)
        # are not included.
        project_name = self.config.get_project_name()
        job_queue_arns = [
            f"arn:{self.partition}:sqs:{region}:{self.account}:{project_name}-jobs-{region}"
            for region in self.config.get_regions()
        ]
        if job_queue_arns:
            self.sagemaker_execution_role.add_to_policy(
                iam.PolicyStatement(
                    effect=iam.Effect.ALLOW,
                    actions=["sqs:SendMessage"],
                    resources=job_queue_arns,
                )
            )

        # ssm:GetParameter on the Cluster_Shared_Bucket metadata params. The
        # three parameters (name/arn/region) live in the global region where
        # GCOGlobalStack is deployed, not in the analytics region; the grant
        # names exactly those three, so a notebook can fetch the bucket name
        # at runtime via
        #   boto3.client('ssm', region_name='<global-region>').get_parameter(
        #       Name='/<project_name>/cluster-shared-bucket/name')['Parameter']['Value']
        # without any JupyterLab-terminal export step.
        global_region = self.config.get_global_region()
        cluster_shared_prefix = cluster_shared_ssm_parameter_prefix(self.project_name)
        self.sagemaker_execution_role.add_to_policy(
            iam.PolicyStatement(
                effect=iam.Effect.ALLOW,
                actions=["ssm:GetParameter", "ssm:GetParameters"],
                resources=[
                    f"arn:{self.partition}:ssm:{global_region}:{self.account}:parameter"
                    f"{cluster_shared_prefix}/{name}"
                    for name in ("name", "arn", "region")
                ],
            )
        )

        # EFS mount actions — scoped to the Studio EFS file-system ARN.
        self.sagemaker_execution_role.add_to_policy(
            iam.PolicyStatement(
                effect=iam.Effect.ALLOW,
                actions=[
                    "elasticfilesystem:ClientMount",
                    "elasticfilesystem:ClientWrite",
                    "elasticfilesystem:ClientRootAccess",
                ],
                resources=[self.studio_efs.file_system_arn],
            )
        )

        # SageMaker calls DescribeMountTargets during user profile
        # provisioning to validate the EFS mount configuration. Granted on
        # Resource: * (acknowledged on the role below).
        self.sagemaker_execution_role.add_to_policy(
            iam.PolicyStatement(
                effect=iam.Effect.ALLOW,
                actions=[
                    "elasticfilesystem:DescribeMountTargets",
                    "elasticfilesystem:DescribeFileSystems",
                ],
                resources=["*"],
            )
        )

        # The Studio UI statement (domain, profile, space and app actions)
        # names this stack's domain id, so ``_create_studio_domain`` adds it
        # once the domain exists.

        # EMR Serverless — allow the execution role to discover, connect to,
        # and manage the EMR Serverless application from Studio's Data panel.
        self.sagemaker_execution_role.add_to_policy(
            iam.PolicyStatement(
                effect=iam.Effect.ALLOW,
                actions=[
                    "emr-serverless:ListApplications",
                    "emr-serverless:GetApplication",
                    "emr-serverless:CreateApplication",
                    "emr-serverless:StartApplication",
                    "emr-serverless:StopApplication",
                    "emr-serverless:StartJobRun",
                    "emr-serverless:GetJobRun",
                    "emr-serverless:ListJobRuns",
                    "emr-serverless:CancelJobRun",
                    "emr-serverless:GetDashboardForJobRun",
                    "emr-serverless:AccessLivyEndpoints",
                ],
                resources=["*"],
            )
        )

        # SageMaker-managed MLflow + Model Registry + MLflow Apps.
        #
        # We attach the AWS-managed ``AmazonSageMakerFullAccess`` policy
        # for two reasons:
        #
        # 1. MLflow Apps (the newer Studio panel, separate from MLflow
        #    Tracking Servers) requires ``sagemaker:CreateMlflowApp``/
        #    ``ListMlflowApps``/``DescribeMlflowApp`` etc. The action
        #    surface is evolving quickly and the managed policy tracks
        #    it. Enumerating it inline would drift.
        # 2. SageMaker Model Registry (``sagemaker:*ModelPackage*``),
        #    Studio space/app lifecycle, training-job submission, and
        #    the "related-services" helpers (S3, ECR, CloudWatch Logs)
        #    are already covered by the managed policy — keeping them
        #    inline duplicated the managed policy and kept us in a
        #    catch-up loop whenever SageMaker shipped a new feature.
        #
        # The managed policy is ``Resource: *`` by design; the trade-off
        # (broader-than-least-privilege inside the role) is
        # acknowledged with a nag suppression below.  The inline
        # ``sagemaker-mlflow:*`` statement that follows is still
        # required because the managed policy does NOT cover the
        # ``sagemaker-mlflow`` data-plane namespace — that's what the
        # MLflow SDK talks to over SigV4 for ``log_metric``,
        # ``log_artifact``, ``register_model``, etc.
        from gco.stacks.nag_suppressions import suppress_managed_policy_opt_in

        self.sagemaker_execution_role.add_managed_policy(
            iam.ManagedPolicy.from_aws_managed_policy_name("AmazonSageMakerFullAccess")
        )
        suppress_managed_policy_opt_in(
            self.sagemaker_execution_role,
            managed_policy_name="AmazonSageMakerFullAccess",
            reason=(
                "AmazonSageMakerFullAccess is attached to "
                "SageMaker_Execution_Role when analytics_environment.enabled=true. "
                "The managed policy covers MLflow Apps, MLflow Tracking "
                "Servers, SageMaker Model Registry, Studio space/app "
                "lifecycle, training-job submission, and the cross-service "
                "helpers (S3, ECR, CloudWatch Logs) SageMaker needs to "
                "render the IDE and run jobs. Enumerating this surface "
                "inline drifts out of date within weeks — tracking the "
                "AWS-managed policy is the supported path. The inline "
                "``sagemaker-mlflow:*`` statement that follows covers "
                "the data-plane namespace the managed policy does not "
                "include. Users who want a locked-down alternative can "
                "disable the analytics environment."
            ),
        )

        # MLflow SDK data-plane (``sagemaker-mlflow:*``) — required for
        # ``mlflow.log_metric``, ``mlflow.log_artifact``,
        # ``mlflow.register_model``, etc. to round-trip through the
        # SageMaker-managed tracking server over SigV4. The managed
        # policy above covers the ``sagemaker:*`` control-plane
        # namespace but not ``sagemaker-mlflow:*`` (a separate service
        # prefix), so we keep this inline and scope it to the
        # api-gateway region where the tracking server and MLflow apps
        # live.
        self.sagemaker_execution_role.add_to_policy(
            iam.PolicyStatement(
                effect=iam.Effect.ALLOW,
                actions=["sagemaker-mlflow:*"],
                resources=[
                    f"arn:{self.partition}:sagemaker:{api_gw_region}:{self.account}:"
                    "mlflow-tracking-server/*",
                    f"arn:{self.partition}:sagemaker:{api_gw_region}:{self.account}:mlflow-app/*",
                ],
            )
        )

        # MLflow's SigV4 plug-in exchanges STS ``GetCallerIdentity`` on
        # every request — the execution role needs that on ``*``.
        # ``sts:GetCallerIdentity`` does not support resource-level
        # scoping, so Resource: * is the only valid value.
        self.sagemaker_execution_role.add_to_policy(
            iam.PolicyStatement(
                effect=iam.Effect.ALLOW,
                actions=["sts:GetCallerIdentity"],
                resources=["*"],
            )
        )

        # HyperPod sub-toggle — additional SageMaker actions for training-job
        # submission and cluster-instance lifecycle management.
        # ``resources=["*"]`` is the documented scope; the HyperPod actions
        # themselves encode the per-training-job authorization model.
        if self.hyperpod_enabled:
            self.sagemaker_execution_role.add_to_policy(
                iam.PolicyStatement(
                    effect=iam.Effect.ALLOW,
                    actions=[
                        "sagemaker:CreateTrainingJob",
                        "sagemaker:DescribeTrainingJob",
                        "sagemaker:StopTrainingJob",
                        "sagemaker:ClusterInstance",
                        "sagemaker:ClusterInstanceGroup",
                        "sagemaker:DescribeClusterNode",
                        "sagemaker:ListClusterNodes",
                    ],
                    resources=["*"],
                )
            )

        # Canvas sub-toggle — attach AWS-managed ``AmazonSageMakerCanvasFullAccess``
        # to the execution role so users can launch the Canvas no-code ML
        # app from inside Studio. The managed policy is used deliberately
        # (rather than enumerating each action) because Canvas's per-feature
        # permission surface — Bedrock for generative AI, Forecast for time
        # series, Rekognition for image classification, S3 writes for
        # datasets, Athena for SQL sources, etc. — is large and evolves with
        # every Canvas release. Tracking AWS's managed policy means we pick
        # up new Canvas capabilities automatically without shipping a CDK
        # change. The trade-off (broader-than-least-privilege inside the
        # role) is acknowledged with a dedicated nag suppression below.
        #
        # The matching ``CanvasAppSettings`` override on the Studio domain
        # lives in ``_create_studio_domain`` so the Canvas tile shows up
        # on the Studio landing page when the toggle is on.
        if self.canvas_enabled:
            self.sagemaker_execution_role.add_managed_policy(
                iam.ManagedPolicy.from_aws_managed_policy_name("AmazonSageMakerCanvasFullAccess")
            )

            suppress_managed_policy_opt_in(
                self.sagemaker_execution_role,
                managed_policy_name="AmazonSageMakerCanvasFullAccess",
                reason=(
                    "AmazonSageMakerCanvasFullAccess is attached to "
                    "SageMaker_Execution_Role when analytics_environment.canvas.enabled=true. "
                    "Canvas is an opt-in sub-toggle (off by default) and its managed "
                    "policy is preferred over an enumerated least-privilege policy "
                    "because Canvas's cross-service permission surface (Bedrock, "
                    "Forecast, Rekognition, Athena, S3 dataset writes, etc.) evolves "
                    "with every Canvas release — tracking the managed policy keeps "
                    "Canvas functional as AWS ships new features. Users who want a "
                    "locked-down alternative can keep the toggle off."
                ),
            )

        # EFS file-system policies authorize NFS client access only. Keep
        # control-plane Describe/Delete permissions on the identities that
        # need them (the execution and cleanup roles), rather than exposing
        # them through a wildcard resource principal. The Studio runtime
        # mounts as its execution role and only through a VPC mount target.
        self.studio_efs.add_to_resource_policy(
            iam.PolicyStatement(
                effect=iam.Effect.ALLOW,
                principals=[self.sagemaker_execution_role],
                actions=[
                    "elasticfilesystem:ClientMount",
                    "elasticfilesystem:ClientWrite",
                    "elasticfilesystem:ClientRootAccess",
                ],
                conditions={
                    "Bool": {"elasticfilesystem:AccessedViaMountTarget": "true"},
                },
            )
        )

        # The wildcards in the statements above, acknowledged on this role
        # only (its default policy is a child, so nothing else is covered).
        from gco.stacks.nag_suppressions import acknowledge_nag_findings

        studio_bucket_id = self.get_logical_id(
            cast(CfnElement, self.studio_only_bucket.node.default_child)
        )
        acknowledge_nag_findings(
            self.sagemaker_execution_role,
            [
                {
                    "id": "AwsSolutions-IAM5",
                    "reason": (
                        "SageMaker_Execution_Role wildcards: (1) the S3 and KMS action "
                        "wildcards grant_read_write and grant_encrypt_decrypt produce, on "
                        "the one Studio_Only_Bucket and Analytics_KMS_Key, and the bucket's "
                        "<arn>/* object keys; (2) execute-api:Invoke on any REST API id "
                        "under prod /api/v1/* and /inference/* in the API Gateway Region, "
                        "because the API Gateway stack depends on this stack and its API "
                        "does not exist when this one deploys; (3) sagemaker-mlflow:* on "
                        "the Region's MLflow tracking servers and apps, which notebooks "
                        "create after deploy (AmazonSageMakerFullAccess does not cover the "
                        "sagemaker-mlflow prefix); (4) one Resource:* statement holding "
                        "sts:GetCallerIdentity (no resource types), the EFS "
                        "DescribeMountTargets and DescribeFileSystems calls SageMaker makes "
                        "when it provisions a user profile, the EMR Serverless actions "
                        "behind Studio's EMR panel (ListApplications takes no resource and "
                        "CreateApplication creates new applications), and, with "
                        "analytics_environment.hyperpod.enabled, the HyperPod training-job "
                        "and cluster-node actions."
                    ),
                    "appliesTo": [
                        "Action::s3:Abort*",
                        "Action::s3:DeleteObject*",
                        "Action::s3:GetBucket*",
                        "Action::s3:GetObject*",
                        "Action::s3:List*",
                        "Action::kms:GenerateDataKey*",
                        "Action::kms:ReEncrypt*",
                        f"Resource::<{studio_bucket_id}.Arn>/*",
                        (
                            f"Resource::arn:<AWS::Partition>:execute-api:{api_gw_region}:"
                            "<AWS::AccountId>:*/prod/*/api/v1/*"
                        ),
                        (
                            f"Resource::arn:<AWS::Partition>:execute-api:{api_gw_region}:"
                            "<AWS::AccountId>:*/prod/*/inference/*"
                        ),
                        "Action::sagemaker-mlflow:*",
                        (
                            f"Resource::arn:<AWS::Partition>:sagemaker:{api_gw_region}:"
                            "<AWS::AccountId>:mlflow-tracking-server/*"
                        ),
                        (
                            f"Resource::arn:<AWS::Partition>:sagemaker:{api_gw_region}:"
                            "<AWS::AccountId>:mlflow-app/*"
                        ),
                        "Resource::*",
                    ],
                },
            ],
        )

    def _grant_sagemaker_role_on_cluster_shared_bucket(self) -> None:
        """Attach RW + KMS on ``Cluster_Shared_Bucket`` to ``SageMaker_Execution_Role``.

        The bucket lives in ``GCOGlobalStack`` in the global region. Its
        ARN is resolved at deploy time by an ``AwsCustomResource`` that
        issues ``ssm:GetParameter`` against the global region, allowed on
        that one parameter's ARN — mirroring the pattern used by
        ``GCORegionalStack._resolve_cluster_shared_bucket_from_ssm``.

        Two statements attach to the role:

        1. S3: ``GetObject``/``PutObject``/``DeleteObject``/``ListBucket``/
           ``GetBucketLocation`` on ``<arn>`` + ``<arn>/*``.
        2. KMS: ``Decrypt``/``GenerateDataKey`` with the conditions the
           regional job role's grant uses (``s3_bucket_kms_conditions``):
           ``kms:ViaService=s3.<global-region>.<AWS::URLSuffix>`` and
           ``kms:EncryptionContext:aws:s3:arn`` naming the bucket or one of
           its objects.

        This is a role-side policy — the bucket policy is owned
        exclusively by ``GCOGlobalStack``.
        """
        from gco.stacks.nag_suppressions import acknowledge_nag_findings

        global_region = self.config.get_global_region()
        parameter_name = f"{cluster_shared_ssm_parameter_prefix(self.project_name)}/arn"

        read_cr = cr.AwsCustomResource(
            self,
            "ReadClusterSharedBucketArn",
            on_create=cr.AwsSdkCall(
                service="SSM",
                action="getParameter",
                parameters={"Name": parameter_name},
                region=global_region,
                physical_resource_id=cr.PhysicalResourceId.of("analytics-cluster-shared-arn"),
            ),
            on_update=cr.AwsSdkCall(
                service="SSM",
                action="getParameter",
                parameters={"Name": parameter_name},
                region=global_region,
                physical_resource_id=cr.PhysicalResourceId.of("analytics-cluster-shared-arn"),
            ),
            # The custom resource reads one parameter with a literal name in
            # the global Region, so its policy names exactly that ARN.
            policy=cr.AwsCustomResourcePolicy.from_sdk_calls(
                resources=[
                    f"arn:{self.partition}:ssm:{global_region}:{self.account}:"
                    f"parameter{parameter_name}"
                ]
            ),
        )

        shared_arn = read_cr.get_response_field("Parameter.Value")

        # Attach the two policy statements as an inline Policy on the role
        # (policy on the role, not the bucket).
        grant = iam.Policy(
            self,
            "SagemakerClusterSharedBucketGrant",
            roles=[self.sagemaker_execution_role],
            statements=[
                iam.PolicyStatement(
                    effect=iam.Effect.ALLOW,
                    actions=[
                        "s3:GetObject",
                        "s3:PutObject",
                        "s3:DeleteObject",
                        "s3:ListBucket",
                        "s3:GetBucketLocation",
                    ],
                    resources=[shared_arn, f"{shared_arn}/*"],
                ),
                iam.PolicyStatement(
                    effect=iam.Effect.ALLOW,
                    actions=["kms:Decrypt", "kms:GenerateDataKey"],
                    resources=["*"],
                    conditions=s3_bucket_kms_conditions(
                        region=global_region, bucket_arn=shared_arn, url_suffix=self.url_suffix
                    ),
                ),
            ],
        )

        # The policy is a sibling of the role, not its child, so it carries
        # its own acknowledgment: the S3 statement's <arn>/* object keys on the
        # one bucket resolved from SSM (the shape of the regional stack's job
        # role grant) and the KMS statement's Resource: *.
        reader_id = self.get_logical_id(
            cast(CfnElement, read_cr.node.find_child("Resource").node.default_child)
        )
        acknowledge_nag_findings(
            grant,
            [
                {
                    "id": "AwsSolutions-IAM5",
                    "reason": (
                        "The SageMaker RW grant on Cluster_Shared_Bucket uses an <arn>/* "
                        "object-key wildcard on the one bucket ARN resolved from SSM "
                        "(CloudFormation-generated name). The KMS statement is Resource:* "
                        "because the cluster-shared key lives in the global stack and is "
                        "not exported to this Region; kms:ViaService limits it to S3 in "
                        "the global Region and kms:EncryptionContext:aws:s3:arn to this "
                        "one bucket."
                    ),
                    "appliesTo": [
                        "Resource::*",
                        f"Resource::<{reader_id}.Parameter.Value>/*",
                    ],
                },
            ],
        )

    # ==================================================================
    # SageMaker Studio domain
    # ==================================================================

    def _create_studio_domain(self) -> None:
        """Create the SageMaker Studio domain bound to the private VPC.

        ``auth_mode=IAM`` + ``app_network_access_type=VpcOnly`` keeps Studio
        traffic on the private subnets.
        ``DefaultUserSettings.ExecutionRole`` points at the role created in
        :meth:`_create_execution_role_and_grants`. ``CustomImages`` is
        intentionally left unset so Studio falls back to the stock AWS-
        published Distribution images (a tested invariant).

        ``CustomFileSystemConfigs`` mounts ``self.studio_efs`` at
        ``/home/sagemaker-user`` — per-user ``/home/<username>`` isolation
        is enforced by the access points that the presigned-URL Lambda
        creates lazily on first login.
        """
        private_subnets = self.vpc.select_subnets(
            subnet_type=ec2.SubnetType.PRIVATE_WITH_EGRESS
        ).subnets

        efs_fs_config = sagemaker.CfnDomain.EFSFileSystemConfigProperty(
            file_system_id=self.studio_efs.file_system_id,
            file_system_path="/home/sagemaker-user",
        )
        efs_custom_fs = sagemaker.CfnDomain.CustomFileSystemConfigProperty(
            efs_file_system_config=efs_fs_config,
        )

        # We also considered adding an ``S3FileSystemConfig`` custom file
        # system that would mount the always-on ``Cluster_Shared_Bucket``
        # under ``/mount/cluster-shared``. aws-cdk-lib exposes the
        # property and CloudFormation synths it cleanly, but the
        # SageMaker Studio service rejects the resource at create time
        # with ``Invalid request provided: S3FileSystemConfig for
        # SageMaker AI Studio is not supported yet.`` — so we ship
        # without the mount. Notebooks access the cluster-shared
        # bucket via ``boto3`` (the SageMaker execution role's
        # cross-region RW grant in
        # :meth:`_grant_sagemaker_role_on_cluster_shared_bucket` already
        # authorizes that path). Revisit this block when SageMaker
        # Studio lights up S3 custom file systems.
        custom_file_systems: list[sagemaker.CfnDomain.CustomFileSystemConfigProperty] = [
            efs_custom_fs,
        ]

        # Security group for Studio compute — allows all outbound so
        # notebooks can reach the internet (pip, git, etc.) via the NAT
        # gateway. SageMaker's default VpcOnly security group only permits
        # NFS traffic, which blocks all internet access from notebooks.
        self.studio_compute_sg = ec2.SecurityGroup(
            self,
            "StudioComputeSg",
            vpc=self.vpc,
            description="Allows outbound internet access from Studio notebooks",
            allow_all_outbound=True,
        )

        default_user_settings = sagemaker.CfnDomain.UserSettingsProperty(
            execution_role=self.sagemaker_execution_role.role_arn,
            custom_file_system_configs=custom_file_systems,
            security_groups=[self.studio_compute_sg.security_group_id],
            # ``jupyter_lab_app_settings`` is deliberately omitted so
            # ``CustomImages`` stays absent — the template contains no
            # SageMaker image resources and no CustomImages
            # key on the domain.
        )

        self.studio_domain = sagemaker.CfnDomain(
            self,
            "StudioDomain",
            auth_mode="IAM",
            app_network_access_type="VpcOnly",
            domain_name=f"{self.project_name}-studio-{self.region}",
            subnet_ids=[s.subnet_id for s in private_subnets],
            vpc_id=self.vpc.vpc_id,
            kms_key_id=self.kms_key.key_id,
            default_user_settings=default_user_settings,
        )

        self._grant_sagemaker_role_on_studio_domain()

        # Canvas sub-toggle (UI side): **IAM-only**. The
        # ``AmazonSageMakerCanvasFullAccess`` managed policy attached to
        # the SageMaker execution role in
        # :meth:`_create_execution_role_and_grants` is sufficient to
        # surface the Canvas tile on the Studio landing page — when a
        # user with that policy opens Studio, SageMaker auto-discovers
        # the entitlement and lights up the Canvas launcher.
        #
        # We intentionally do *not* inject a
        # ``DefaultUserSettings.CanvasAppSettings`` block on the domain.
        # The CloudFormation ``AWS::SageMaker::Domain`` resource does
        # not accept that property (only ``AWS::SageMaker::UserProfile``
        # does), so a property override fails early validation with
        # ``Unsupported property [CanvasAppSettings]``. Canvas uses its
        # own default workspace artifact locations; operators who want
        # to pin per-user Canvas defaults can apply
        # ``CanvasAppSettings`` at the ``UserProfile`` level directly.

        # The domain validates that the EFS file system has mount targets in
        # every subnet before stabilizing. CDK doesn't infer this dependency
        # from the file_system_id reference alone, so we add it explicitly.
        self.studio_domain.node.add_dependency(self.studio_efs)

        CfnOutput(
            self,
            "StudioDomainName",
            value=self.studio_domain.domain_name or "",
            description="Name of the SageMaker Studio domain",
        )

        # Cleanup custom resource — on stack deletion, removes all user
        # profiles from the domain and all access points from the EFS so
        # CloudFormation can delete the domain and file system cleanly.
        from aws_cdk import CustomResource
        from aws_cdk import custom_resources as cr_provider

        cleanup_fn = lambda_.Function(
            self,
            "CleanupFunction",
            runtime=getattr(lambda_.Runtime, LAMBDA_PYTHON_RUNTIME),
            handler="handler.handler",
            code=lambda_source_code("lambda/analytics-cleanup"),
            # 15 minutes covers the worst case of multiple async drain
            # loops in series: apps (up to ~2 min), spaces (up to ~3 min),
            # user profiles (up to ~3 min), SageMaker-managed EFS mount
            # targets (up to ~2 min), plus incidental RPC latency and
            # security-group cleanup. In the common case (a handful of
            # users) this finishes in well under a minute.
            timeout=Duration.minutes(15),
            memory_size=128,
            environment={
                "DOMAIN_ID": self.studio_domain.attr_domain_id,
                "EFS_ID": self.studio_efs.file_system_id,
                "REGION": self.region,
                "VPC_ID": self.vpc.vpc_id,
            },
        )

        # Use a customer-managed policy instead of an inline policy.
        # Inline policies (created by add_to_role_policy) are separate
        # CloudFormation resources that can be deleted before the custom
        # resource fires during stack deletion. A managed policy attached
        # via the role's managedPolicies property is part of the role
        # resource itself and persists until the role is deleted.
        #
        # Each statement names what the handler (lambda/analytics-cleanup)
        # touches, as far as the template knows it:
        # * the List calls and DescribeSecurityGroups take no resource;
        # * the app, space and profile deletes and DescribeDomain name this
        #   domain (the app, space and profile names are created at run time);
        # * the access-point lookup names Studio_EFS; the access points are
        #   created at run time, so DeleteAccessPoint takes this Region's
        #   access points;
        # * the home file system SageMaker creates for the domain (its id is
        #   known only from DescribeDomain at run time) needs its policy,
        #   mount targets and itself deleted, so those take this Region's file
        #   systems (DeleteFileSystemPolicy also covers Studio_EFS);
        # * SageMaker's NFS security groups are revoked and deleted only in
        #   this stack's VPC (ec2:Vpc).
        domain_id = self.studio_domain.attr_domain_id
        sagemaker_arn_prefix = f"arn:{self.partition}:sagemaker:{self.region}:{self.account}"
        efs_arn_prefix = f"arn:{self.partition}:elasticfilesystem:{self.region}:{self.account}"
        cleanup_policy = iam.ManagedPolicy(
            self,
            "CleanupFunctionPolicy",
            statements=[
                iam.PolicyStatement(
                    effect=iam.Effect.ALLOW,
                    actions=[
                        "sagemaker:ListApps",
                        "sagemaker:ListSpaces",
                        "sagemaker:ListUserProfiles",
                        "ec2:DescribeSecurityGroups",
                    ],
                    resources=["*"],
                ),
                iam.PolicyStatement(
                    effect=iam.Effect.ALLOW,
                    actions=[
                        "sagemaker:DescribeDomain",
                        "sagemaker:DeleteApp",
                        "sagemaker:DeleteSpace",
                        "sagemaker:DeleteUserProfile",
                    ],
                    resources=[
                        f"{sagemaker_arn_prefix}:domain/{domain_id}",
                        f"{sagemaker_arn_prefix}:app/{domain_id}/*",
                        f"{sagemaker_arn_prefix}:space/{domain_id}/*",
                        f"{sagemaker_arn_prefix}:user-profile/{domain_id}/*",
                    ],
                ),
                iam.PolicyStatement(
                    effect=iam.Effect.ALLOW,
                    actions=["elasticfilesystem:DescribeAccessPoints"],
                    resources=[self.studio_efs.file_system_arn],
                ),
                iam.PolicyStatement(
                    effect=iam.Effect.ALLOW,
                    actions=["elasticfilesystem:DeleteAccessPoint"],
                    resources=[f"{efs_arn_prefix}:access-point/*"],
                ),
                iam.PolicyStatement(
                    effect=iam.Effect.ALLOW,
                    actions=[
                        "elasticfilesystem:DeleteFileSystemPolicy",
                        "elasticfilesystem:DescribeMountTargets",
                        "elasticfilesystem:DeleteMountTarget",
                        "elasticfilesystem:DeleteFileSystem",
                    ],
                    resources=[f"{efs_arn_prefix}:file-system/*"],
                ),
                iam.PolicyStatement(
                    effect=iam.Effect.ALLOW,
                    actions=[
                        "ec2:RevokeSecurityGroupIngress",
                        "ec2:RevokeSecurityGroupEgress",
                        "ec2:DeleteSecurityGroup",
                    ],
                    resources=[
                        f"arn:{self.partition}:ec2:{self.region}:{self.account}:security-group/*"
                    ],
                    conditions={"ArnEquals": {"ec2:Vpc": self.vpc.vpc_arn}},
                ),
            ],
        )
        assert cleanup_fn.role is not None
        cleanup_fn.role.add_managed_policy(cleanup_policy)

        cleanup_provider = cr_provider.Provider(
            self,
            "CleanupProvider",
            on_event_handler=cleanup_fn,
        )

        cleanup_resource = CustomResource(
            self,
            "DomainCleanup",
            service_token=cleanup_provider.service_token,
        )

        # The managed policy must not be deleted until after the cleanup
        # custom resource completes. Adding a dependency ensures
        # CloudFormation keeps the policy alive during the Lambda execution.
        cleanup_resource.node.add_dependency(cleanup_policy)

        # Store reference so _create_presigned_url_lambda can add a
        # dependency after it creates the presigned-URL Lambda.
        self._cleanup_resource = cleanup_resource

        # The cleanup policy is a sibling of the role, so its wildcards are
        # acknowledged on the policy itself (see the statement comments above).
        from gco.stacks.nag_suppressions import acknowledge_nag_findings

        domain = f"<{self.get_logical_id(self.studio_domain)}.DomainId>"
        nag_prefix = "Resource::arn:<AWS::Partition>:{service}:" + self.region + ":<AWS::AccountId>"
        acknowledge_nag_findings(
            cleanup_policy,
            [
                {
                    "id": "AwsSolutions-IAM5",
                    "reason": (
                        "The stack-deletion cleanup Lambda: sagemaker:ListApps, "
                        "ListSpaces, ListUserProfiles and ec2:DescribeSecurityGroups "
                        "take no resource (Resource:*); the app, space and user-profile "
                        "deletes name this domain, under which the names are created "
                        "at run time; DeleteAccessPoint takes this Region's access "
                        "points and the home file system's policy, mount-target and "
                        "file-system deletes take this Region's file systems, because "
                        "SageMaker creates them at run time and reports their ids only "
                        "through DescribeDomain; the security-group revokes and deletes "
                        "are limited to this stack's VPC by ec2:Vpc."
                    ),
                    "appliesTo": [
                        "Resource::*",
                        nag_prefix.format(service="sagemaker") + f":app/{domain}/*",
                        nag_prefix.format(service="sagemaker") + f":space/{domain}/*",
                        nag_prefix.format(service="sagemaker") + f":user-profile/{domain}/*",
                        nag_prefix.format(service="elasticfilesystem") + ":access-point/*",
                        nag_prefix.format(service="elasticfilesystem") + ":file-system/*",
                        nag_prefix.format(service="ec2") + ":security-group/*",
                    ],
                },
            ],
        )
        assert cleanup_fn.role is not None  # always set for non-imported functions
        acknowledge_nag_findings(
            cleanup_fn.role,
            [
                {
                    "id": "AwsSolutions-IAM4",
                    "reason": (
                        "Cleanup Lambda uses AWSLambdaBasicExecutionRole "
                        "managed policy for CloudWatch Logs access."
                    ),
                    "appliesTo": [
                        "Policy::arn:<AWS::Partition>:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole",
                    ],
                },
            ],
        )
        acknowledge_nag_findings(
            cleanup_provider,
            [
                {
                    "id": "AwsSolutions-IAM5",
                    "reason": (
                        "The CDK Provider framework's invoke policy names the cleanup "
                        "function's ARN and its versions (<arn>:*)."
                    ),
                    "appliesTo": [
                        "Resource::<CleanupFunction1604930F.Arn>:*",
                    ],
                },
                {
                    "id": "AwsSolutions-IAM4",
                    "reason": ("CDK Provider framework uses AWSLambdaBasicExecutionRole."),
                    "appliesTo": [
                        "Policy::arn:<AWS::Partition>:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole",
                    ],
                },
            ],
        )

    # ==================================================================
    # EMR Serverless application
    # ==================================================================

    def _grant_sagemaker_role_on_studio_domain(self) -> None:
        """Grant the execution role the Studio UI actions on this stack's domain.

        The Studio runtime assumes the role to render the IDE and to create,
        update and delete its spaces and apps. Domain, user-profile, space and
        app ARNs all carry the domain id, so the grant names this domain only;
        the profile, space and app names are the remaining wildcards (they are
        created at run time). ``ListSpaces`` and ``ListApps`` take no resource,
        so they are not here: ``AmazonSageMakerFullAccess`` grants them, with
        the Describe actions, on ``Resource: *``.
        """
        from gco.stacks.nag_suppressions import acknowledge_nag_findings

        domain_id = self.studio_domain.attr_domain_id
        arn_prefix = f"arn:{self.partition}:sagemaker:{self.region}:{self.account}"
        self.sagemaker_execution_role.add_to_policy(
            iam.PolicyStatement(
                effect=iam.Effect.ALLOW,
                actions=[
                    "sagemaker:DescribeDomain",
                    "sagemaker:DescribeUserProfile",
                    "sagemaker:CreatePresignedDomainUrl",
                    "sagemaker:DescribeApp",
                    "sagemaker:DescribeSpace",
                    "sagemaker:CreateApp",
                    "sagemaker:DeleteApp",
                    "sagemaker:CreateSpace",
                    "sagemaker:DeleteSpace",
                    "sagemaker:UpdateSpace",
                    "sagemaker:ListTags",
                    "sagemaker:AddTags",
                ],
                resources=[
                    f"{arn_prefix}:domain/{domain_id}",
                    f"{arn_prefix}:user-profile/{domain_id}/*",
                    f"{arn_prefix}:space/{domain_id}/*",
                    f"{arn_prefix}:app/{domain_id}/*/*/*",
                ],
            )
        )
        domain = f"<{self.get_logical_id(self.studio_domain)}.DomainId>"
        nag_prefix = f"Resource::arn:<AWS::Partition>:sagemaker:{self.region}:<AWS::AccountId>"
        acknowledge_nag_findings(
            self.sagemaker_execution_role,
            [
                {
                    "id": "AwsSolutions-IAM5",
                    "reason": (
                        "The Studio UI actions name this stack's domain; the user "
                        "profile, space and app names under it are created at run "
                        "time, so they are the wildcards."
                    ),
                    "appliesTo": [
                        f"{nag_prefix}:user-profile/{domain}/*",
                        f"{nag_prefix}:space/{domain}/*",
                        f"{nag_prefix}:app/{domain}/*/*/*",
                    ],
                },
            ],
        )

    def _create_emr_app(self) -> None:
        """Create an EMR Serverless Spark application on the private VPC.

        Pinned ``release_label`` lives in
        ``gco.stacks.constants.EMR_SERVERLESS_RELEASE_LABEL`` so analytics
        workloads get a reproducible Spark runtime across deployments. The
        application's network configuration uses the private
        subnets + a dedicated security group so Spark workers stay on the
        same network perimeter as the Studio notebooks.
        """
        private_subnet_ids = [
            s.subnet_id
            for s in self.vpc.select_subnets(subnet_type=ec2.SubnetType.PRIVATE_WITH_EGRESS).subnets
        ]

        self.emr_security_group = ec2.SecurityGroup(
            self,
            "EmrServerlessSecurityGroup",
            vpc=self.vpc,
            description="SG for EMR Serverless Spark workers",
            allow_all_outbound=True,
        )

        self.emr_app = emrserverless.CfnApplication(
            self,
            "EmrServerlessApp",
            name=f"{self.project_name}-spark-{self.region}",
            release_label=EMR_SERVERLESS_RELEASE_LABEL,
            type="SPARK",
            network_configuration=emrserverless.CfnApplication.NetworkConfigurationProperty(
                subnet_ids=private_subnet_ids,
                security_group_ids=[self.emr_security_group.security_group_id],
            ),
        )

    # ==================================================================
    # Cognito pool + client + domain
    # ==================================================================

    def _create_cognito_pool(self) -> None:
        """Create the Cognito user pool that authenticates SageMaker Studio logins.

        Password policy, standard threat-protection mode, and self-sign-up-
        disabled flags are configured for SRP-backed Studio logins. The
        attached ``UserPoolClient`` runs SRP auth
        (used by ``gco analytics studio login``) with token revocation
        enabled. The ``UserPoolDomain`` uses the configurable prefix from
        ``analytics_environment.cognito.domain_prefix`` or defaults to
        ``gco-studio-<account>``.
        """
        self.cognito_pool = cognito.UserPool(
            self,
            "StudioUserPool",
            self_sign_up_enabled=False,
            password_policy=cognito.PasswordPolicy(
                min_length=12,
                require_digits=True,
                require_symbols=True,
                require_uppercase=True,
                require_lowercase=True,
            ),
            sign_in_aliases=cognito.SignInAliases(username=True),
            auto_verify=cognito.AutoVerifiedAttrs(email=True),
            # Replaces the deprecated ``advanced_security_mode`` kwarg
            # (aws-cdk-lib's AdvancedSecurityMode enum is gone as of the
            # Cognito November 2024 tier changes). Lite feature plan — the
            # default — does not support real threat protection, so we set
            # ``NO_ENFORCEMENT`` here to keep the synth warning-free.
            # TODO: operators who want real threat protection should opt
            # into the Essentials or Plus feature plan by also setting
            # ``feature_plan=cognito.FeaturePlan.ESSENTIALS`` (or
            # ``FeaturePlan.PLUS``) and flipping this to
            # ``StandardThreatProtectionMode.FULL_FUNCTION``. That path
            # changes the per-MAU price — see the Cognito pricing doc —
            # which is why the default stays on Lite+NO_ENFORCEMENT.
            standard_threat_protection_mode=(cognito.StandardThreatProtectionMode.NO_ENFORCEMENT),
            removal_policy=self.cognito_removal,
        )
        self._tag_retainable(self.cognito_pool)

        from gco.stacks.nag_suppressions import acknowledge_nag_findings

        # cdk-nag AwsSolutions-COG8 (new in cdk-nag 2.38.x): the Lite feature plan is intentional (cost); see UserPool above.
        acknowledge_nag_findings(
            self.cognito_pool,
            [
                {
                    "id": "AwsSolutions-COG8",
                    "reason": "Studio user pool intentionally uses the Lite feature plan with NO_ENFORCEMENT threat protection to avoid the Plus plan per-MAU cost; it only gates internal SageMaker Studio access. Operators who need threat protection can opt into the Essentials/Plus feature plan as documented on the UserPool definition.",
                }
            ],
        )
        self.cognito_client = self.cognito_pool.add_client(
            "StudioUserPoolClient",
            auth_flows=cognito.AuthFlow(
                user_srp=True,
                admin_user_password=True,
            ),
            prevent_user_existence_errors=True,
            enable_token_revocation=True,
        )

        # Domain prefix — default is ``<project_name>-studio-<account>`` (from
        # constants.cognito_domain_prefix_default(project_name) + account suffix).
        # The override in cdk.json is used verbatim when non-None, without
        # appending the account id, because operators who override the
        # prefix typically want a short memorable value.
        if self._cognito_domain_prefix_override:
            domain_prefix = self._cognito_domain_prefix_override
        else:
            domain_prefix = f"{cognito_domain_prefix_default(self.project_name)}-{self.account}"

        self.cognito_domain = self.cognito_pool.add_domain(
            "StudioUserPoolDomain",
            cognito_domain=cognito.CognitoDomainOptions(domain_prefix=domain_prefix),
        )

        CfnOutput(
            self,
            "CognitoUserPoolId",
            value=self.cognito_pool.user_pool_id,
            description="ID of the Cognito user pool that gates SageMaker Studio",
        )
        CfnOutput(
            self,
            "CognitoUserPoolArn",
            value=self.cognito_pool.user_pool_arn,
            description="ARN of the Cognito user pool",
        )
        CfnOutput(
            self,
            "CognitoUserPoolClientId",
            value=self.cognito_client.user_pool_client_id,
            description="Client ID used by the GCO CLI for SRP auth",
        )

    # ==================================================================
    # Presigned-URL Lambda
    # ==================================================================

    def _create_presigned_url_lambda(self) -> None:
        """Create the ``Presigned_URL_Lambda`` that mints Studio login URLs.

        Wired into API Gateway's ``/studio/login`` route from
        ``GCOApiGatewayGlobalStack``. The function lives on
        ``GCOAnalyticsStack`` (not the API gateway stack) so its IAM role
        can reference ``SageMaker_Execution_Role.role_arn`` on ``PassRole``
        and ``Studio_EFS.file_system_arn`` on the EFS access-point actions
        without a cross-stack import.

        Key configuration:

        * Runtime: ``LAMBDA_PYTHON_RUNTIME`` from ``gco.stacks.constants``.
        * Timeout: 29 s — API Gateway's maximum integration timeout is 29
          seconds, so matching it here lets the Lambda time out *before*
          API Gateway does, producing a clean HTTP 500 with our opaque
          error token rather than API Gateway's 504.
        * Tracing: ``ACTIVE`` so X-Ray captures the
          ``sagemaker:CreatePresignedDomainUrl`` call.
        * Log group retention: 1 month.

        IAM scoping:

        * ``sagemaker:DescribeDomain`` + ``CreatePresignedDomainUrl`` +
          ``DescribeUserProfile`` + ``CreateUserProfile`` +
          ``DeleteUserProfile`` + ``ListTags`` + ``AddTags`` on this
          stack's Studio domain and its user profiles
          (``user-profile/<domain-id>/*``: a profile is named after the
          Cognito user and created at first login). ``DeleteUserProfile``
          lets the handler replace a ``Failed`` profile. The handler reads
          the domain id from ``STUDIO_DOMAIN_ID``, so it lists no domains.
        * ``iam:PassRole`` on ``SageMaker_Execution_Role.role_arn`` with a
          ``StringEquals iam:PassedToService=sagemaker.amazonaws.com``
          condition so the role can only ever be handed to SageMaker.
        * ``elasticfilesystem:DescribeAccessPoints`` +
          ``CreateAccessPoint`` on ``Studio_EFS.file_system_arn`` for the
          lazy per-user access-point creation path in the handler.
        * ``AWSLambdaBasicExecutionRole`` managed policy for the CloudWatch
          Logs + X-Ray write path.
        """
        from gco.stacks.nag_suppressions import acknowledge_nag_findings

        # Dedicated IAM role — narrow-scoped, no reuse across other
        # Lambdas. We attach the basic execution role as a managed policy
        # so the nag rule for ``AwsSolutions-IAM4`` is happy; everything
        # else is an inline policy we own entirely.
        self.presigned_url_lambda_role = iam.Role(
            self,
            "PresignedUrlLambdaRole",
            assumed_by=iam.ServicePrincipal("lambda.amazonaws.com"),
            description=(
                "Execution role for the analytics presigned-URL Lambda. "
                "Scoped to SageMaker domain + user-profile operations, "
                "PassRole on SageMaker_Execution_Role, and EFS access-"
                "point management on Studio_EFS."
            ),
            managed_policies=[
                iam.ManagedPolicy.from_aws_managed_policy_name(
                    "service-role/AWSLambdaBasicExecutionRole"
                )
            ],
        )

        # Domain + user-profile actions on this stack's Studio domain. A
        # profile is named after the Cognito user and created at first
        # login, so the profile name is the one wildcard.
        # DeleteUserProfile lets the handler replace a Failed profile.
        domain_id = self.studio_domain.attr_domain_id
        sagemaker_arn_prefix = f"arn:{self.partition}:sagemaker:{self.region}:{self.account}"
        self.presigned_url_lambda_role.add_to_policy(
            iam.PolicyStatement(
                effect=iam.Effect.ALLOW,
                actions=[
                    "sagemaker:DescribeDomain",
                    "sagemaker:CreatePresignedDomainUrl",
                    "sagemaker:DescribeUserProfile",
                    "sagemaker:CreateUserProfile",
                    "sagemaker:DeleteUserProfile",
                    "sagemaker:ListTags",
                    "sagemaker:AddTags",
                ],
                resources=[
                    f"{sagemaker_arn_prefix}:domain/{domain_id}",
                    f"{sagemaker_arn_prefix}:user-profile/{domain_id}/*",
                ],
            )
        )

        # iam:PassRole — only SageMaker_Execution_Role, only to
        # sagemaker.amazonaws.com. This is what CreateUserProfile passes
        # on the ``ExecutionRole`` field.
        self.presigned_url_lambda_role.add_to_policy(
            iam.PolicyStatement(
                effect=iam.Effect.ALLOW,
                actions=["iam:PassRole"],
                resources=[self.sagemaker_execution_role.role_arn],
                conditions={
                    "StringEquals": {
                        "iam:PassedToService": "sagemaker.amazonaws.com",
                    }
                },
            )
        )

        # EFS access-point management — scoped to the Studio_EFS file
        # system. The Lambda creates one access point per Cognito user
        # at first login (lazy-in-Lambda approach).
        self.presigned_url_lambda_role.add_to_policy(
            iam.PolicyStatement(
                effect=iam.Effect.ALLOW,
                actions=[
                    "elasticfilesystem:DescribeAccessPoints",
                    "elasticfilesystem:CreateAccessPoint",
                    "elasticfilesystem:TagResource",
                ],
                resources=[self.studio_efs.file_system_arn],
            )
        )

        # CloudWatch log group with 1-month retention. We own
        # the group explicitly (rather than letting Lambda auto-create
        # one) so the retention setting is captured in the template.
        presigned_url_log_group = logs.LogGroup(
            self,
            "PresignedUrlLambdaLogGroup",
            retention=logs.RetentionDays.ONE_MONTH,
            removal_policy=RemovalPolicy.DESTROY,
        )

        self.presigned_url_lambda = lambda_.Function(
            self,
            "PresignedUrlFunction",
            runtime=getattr(lambda_.Runtime, LAMBDA_PYTHON_RUNTIME),
            handler=PRESIGNED_URL_HANDLER,
            code=lambda_source_code("lambda/analytics-presigned-url"),
            role=self.presigned_url_lambda_role,
            timeout=Duration.seconds(PRESIGNED_URL_TIMEOUT_SECONDS),
            memory_size=PRESIGNED_URL_MEMORY_MB,
            tracing=lambda_.Tracing.ACTIVE,
            log_group=presigned_url_log_group,
            description=(
                "Exchanges a Cognito-authorized event for a presigned "
                "SageMaker Studio URL. Wired into /studio/login by "
                "GCOApiGatewayGlobalStack."
            ),
            environment={
                "STUDIO_DOMAIN_ID": self.studio_domain.attr_domain_id,
                "SAGEMAKER_EXECUTION_ROLE_ARN": self.sagemaker_execution_role.role_arn,
                "STUDIO_EFS_ID": self.studio_efs.file_system_id,
                "URL_EXPIRES_SECONDS": "300",
                "SESSION_EXPIRES_SECONDS": "43200",
            },
        )

        CfnOutput(
            self,
            "PresignedUrlLambdaArn",
            value=self.presigned_url_lambda.function_arn,
            description=(
                "ARN of the presigned-URL Lambda - consumed by the API "
                "Gateway stack's /studio/login integration."
            ),
        )

        # Nag suppressions. Each one carries a literal-ARN or documented
        # wildcard ``applies_to`` and a ``reason`` string explaining why
        # tighter scoping isn't possible.
        acknowledge_nag_findings(
            self.presigned_url_lambda_role,
            [
                {
                    "id": "AwsSolutions-IAM5",
                    "reason": (
                        "The SageMaker profile actions take user-profile/<domain-id>/* "
                        "on this stack's Studio domain: each profile is named after "
                        "the Cognito user and created at first login, so its name is "
                        "known only at invoke time. Resource: * is only on the X-Ray "
                        "actions that active tracing adds, which take no resource."
                    ),
                    "appliesTo": [
                        "Resource::*",
                        (
                            "Resource::arn:<AWS::Partition>:sagemaker:<AWS::Region>:"
                            "<AWS::AccountId>:user-profile/<StudioDomain.DomainId>/*"
                        ),
                    ],
                },
            ],
        )

        # The cleanup custom resource must fire AFTER the presigned-URL
        # Lambda is deleted during stack destruction. Otherwise the Lambda
        # can recreate user profiles (via in-flight login requests) between
        # cleanup and domain deletion. Adding the dependency here (after
        # the Lambda is created) ensures correct deletion ordering.
        self._cleanup_resource.node.add_dependency(self.presigned_url_lambda)

    # ==================================================================
    # Nag suppressions
    # ==================================================================

    def _apply_nag_suppressions(self) -> None:
        """Dispatch to the analytics branch in ``gco/stacks/nag_suppressions.py``.

        The analytics branch calls ``add_storage_suppressions`` (EFS backup,
        log-group encryption), ``add_cognito_suppressions``,
        ``add_analytics_vpc_suppressions``, ``add_analytics_s3_suppressions``
        (bucket replication), ``add_lambda_suppressions`` (the stack's
        Lambda functions) and ``add_iam_suppressions`` (CDK custom
        resources). The wildcards of the SageMaker execution role, its
        cluster-shared grant and the cleanup policy are acknowledged on each
        of them where they are created.
        """
        apply_all_suppressions(
            self,
            stack_type="analytics",
            regions=None,
            global_region=self.config.get_global_region(),
            project_name=self.project_name,
        )
