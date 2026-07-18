"""
Global API Gateway stack - Single authenticated entry point for all regions.

This stack creates the centralized API Gateway that serves as the authenticated
entry point for all GCO API requests. It provides:
- Edge-optimized endpoint in the commercial ``aws`` partition; regional endpoint elsewhere
- IAM authentication (AWS SigV4) for all requests
- Global Accelerator-backed HMAC proxy routes in ``aws`` only
- SigV4-authenticated aggregation through regional API Gateway bridges
- Secrets Manager signing key with automatic rotation
- Multi-region replication for the signing key
- CloudWatch logging for audit and debugging

Security Flow in the commercial ``aws`` partition:
    1. Client signs request with AWS credentials (SigV4)
    2. CloudFront edge location receives request (managed by AWS)
    3. API Gateway validates IAM permissions
    4. Lambda proxy retrieves the signing key from Secrets Manager
    5. Lambda signs the method, target, body digest, timestamp, and random nonce
    6. Request is forwarded to Global Accelerator with the HMAC envelope
    7. Backend middleware validates freshness, integrity, and replay protection

Secret Rotation:
    The signing key is automatically rotated daily. During rotation:
    - A new key is generated and stored as AWSPENDING
    - Backend services accept signatures from AWSCURRENT and AWSPENDING keys
    - After validation, AWSPENDING becomes AWSCURRENT
    - Multi-region replication ensures all regions receive the new key

Outside ``aws``, the Global Accelerator proxy Lambdas and catch-all workload
routes are omitted. The regional global API retains authenticated aggregate
routes, while callers use IAM-authenticated regional bridges for workload
control and inference.

The HMAC envelope authenticates each request but does not encrypt its payload;
transport confidentiality is a separate property of the network path. Direct
requests to Global Accelerator (when present) or regional ALBs cannot mint a
valid envelope.
"""

import json
from dataclasses import dataclass
from typing import Any

from aws_cdk import (
    CfnOutput,
    CustomResource,
    Duration,
    Fn,
    RemovalPolicy,
    Stack,
)
from aws_cdk import aws_apigateway as apigateway
from aws_cdk import aws_cloudwatch as cloudwatch
from aws_cdk import aws_cognito as cognito
from aws_cdk import aws_ecr_assets as ecr_assets
from aws_cdk import aws_events as events
from aws_cdk import aws_events_targets as events_targets
from aws_cdk import aws_iam as iam
from aws_cdk import aws_kms as kms
from aws_cdk import aws_lambda as lambda_
from aws_cdk import aws_logs as logs
from aws_cdk import aws_secretsmanager as secretsmanager
from aws_cdk import aws_sqs as sqs
from aws_cdk import aws_wafv2 as wafv2
from aws_cdk import custom_resources as cr
from constructs import Construct

from gco.stacks.constants import (
    AGGREGATOR_REGIONAL_API_ROUTES,
    DEFAULT_MAX_REQUEST_BODY_BYTES,
    LAMBDA_NODEJS_RUNTIME,
    LAMBDA_PYTHON_RUNTIME,
    api_gateway_auth_secret_name,
    backend_tls_certificate_parameter_prefix,
    backend_tls_root_ca_parameter_name,
    backend_tls_root_secret_name,
    backend_tls_server_name,
    cross_region_aggregator_role_name,
    validated_request_body_limit,
)

# <pyflowchart-code-diagram> BEGIN - auto-inserted, do not edit
# Generated at (UTC): 2026-07-18T01:03:40Z
# Flowchart(s) generated from this file:
#   * ``GCOApiGatewayGlobalStack.__init__`` -> ``diagrams/code_diagrams/gco/stacks/api_gateway_global_stack.GCOApiGatewayGlobalStack___init__.html``
#     (PNG: ``diagrams/code_diagrams/gco/stacks/api_gateway_global_stack.GCOApiGatewayGlobalStack___init__.png``)
# Regenerate with ``python diagrams/code_diagrams/generate.py``.
# <pyflowchart-code-diagram> END


@dataclass(frozen=True)
class AnalyticsApiConfig:
    """Configuration handed from ``GCOAnalyticsStack`` to ``GCOApiGatewayGlobalStack``.

    When ``GCOApiGatewayGlobalStack`` is constructed (or mutated via
    :meth:`GCOApiGatewayGlobalStack.set_analytics_config`) with a non-``None``
    instance of this dataclass, the stack wires a Cognito-authorized
    ``/studio/*`` route tree onto the existing REST API. When the value is
    ``None``, the stack is behaviorally identical to its pre-analytics shape
    — no ``/studio/*`` resources, no Cognito authorizer, no additional
    ``CfnOutput`` entries.

    ``frozen=True`` makes the dataclass hashable and immutable so a single
    config object can be safely shared across constructs without the risk
    of accidental mutation after the synthesized template references its
    fields.

    Attributes:
        user_pool_arn: Full ARN of the Cognito user pool that authenticates
            Studio logins. Shape:
            ``arn:aws:cognito-idp:<region>:<account>:userpool/<pool-id>``.
        user_pool_client_id: Client id of the Studio user-pool client
            (SRP auth). Used by the CLI's ``gco analytics studio login``
            flow and surfaced to API Gateway outputs for discoverability.
        presigned_url_lambda: The ``analytics-presigned-url`` Lambda
            function created by ``GCOAnalyticsStack._create_presigned_url_lambda``.
            Consumed by the ``/studio/login`` ``LambdaIntegration``.
        studio_domain_name: SageMaker Studio domain name. Carried through
            as context for the Lambda integration; the Lambda itself also
            reads this value from its ``STUDIO_DOMAIN_NAME`` environment
            variable set by the analytics stack.
        callback_url: Concrete OAuth redirect target
            (``https://<api>/prod/studio/callback``) used when the
            Cognito hosted UI is enabled. The ``/studio/callback`` route
            is wired as a stub here so the URL is reachable immediately
            after deploy.
    """

    user_pool_arn: str
    user_pool_client_id: str
    presigned_url_lambda: lambda_.IFunction
    studio_domain_name: str
    callback_url: str


class GCOApiGatewayGlobalStack(Stack):
    """
    Global API Gateway with IAM authentication.

    This stack creates the single authenticated entry point for all GCO
    API requests. All requests must be signed with AWS credentials.

    Attributes:
        secret: Secrets Manager secret containing the backend HMAC signing key
        proxy_lambda: Buffered Lambda proxy for the control-plane API
        inference_proxy_lambda: Response-streaming Lambda for `/inference/*`
        aggregator_lambda: Lambda function for cross-region aggregation
        api: REST API with IAM authentication
    """

    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        global_accelerator_dns: str | None,
        regional_endpoints: dict[str, str] | None = None,
        analytics_config: AnalyticsApiConfig | None = None,
        project_name: str = "gco",
        api_gateway_config: dict[str, Any] | None = None,
        registry_region: str | None = None,
        certificate_regions: list[str] | None = None,
        backend_tls_config: dict[str, Any] | None = None,
        max_request_body_bytes: int = DEFAULT_MAX_REQUEST_BODY_BYTES,
        **kwargs: Any,
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)

        # ``project_name`` is the deployment's unique prefix. Every physical
        # resource name this stack owns (secret, WAF, log groups, CFN exports)
        # derives from it so two deployments can coexist in one account+region.
        # Defaults to ``"gco"`` so the rendered names are byte-for-byte
        # identical to the pre-#139 literals for the stock deployment.
        self.project_name = project_name
        self.ga_dns = str(global_accelerator_dns).strip() if global_accelerator_dns else None
        self.regional_endpoints = regional_endpoints or {}
        # Regional ALB hostnames and backend-TLS public metadata are registered
        # in the global stack's SSM region, which may differ from this stack.
        self.registry_region = registry_region or self.region
        self.certificate_regions = tuple(
            dict.fromkeys(certificate_regions if certificate_regions is not None else ["us-east-1"])
        )
        default_backend_tls_config: dict[str, int] = {
            "root_generation": 1,
            "root_validity_days": 3_650,
            "root_rotate_before_days": 180,
            "root_activation_delay_hours": 24,
            "root_overlap_days": 45,
            "leaf_validity_days": 30,
            "leaf_rotate_before_days": 10,
            "rotation_schedule_hours": 12,
            "trust_cache_ttl_seconds": 300,
            "trust_cache_max_stale_seconds": 3_600,
        }
        self.backend_tls_config = {
            **default_backend_tls_config,
            **(backend_tls_config or {}),
        }
        self.backend_tls_server_name = backend_tls_server_name(self.project_name)
        self.backend_tls_root_ca_parameter_name = backend_tls_root_ca_parameter_name(
            self.project_name
        )
        self.backend_tls_certificate_parameter_prefix = backend_tls_certificate_parameter_prefix(
            self.project_name
        )
        self.max_request_body_bytes = validated_request_body_limit(max_request_body_bytes)

        default_api_gateway_config: dict[str, Any] = {
            "throttle_rate_limit": 1000,
            "throttle_burst_limit": 2000,
            "log_level": "INFO",
            "metrics_enabled": True,
            "tracing_enabled": True,
        }
        if api_gateway_config is not None:
            configured_api_gateway = api_gateway_config
        else:
            context_config = self.node.try_get_context("api_gateway")
            configured_api_gateway = context_config if isinstance(context_config, dict) else {}
        self.api_gateway_config = {
            **default_api_gateway_config,
            **configured_api_gateway,
        }
        # When analytics is disabled (the default) this stays ``None`` and
        # the stack synthesizes exactly as it did pre-analytics. When
        # non-``None``, ``_wire_studio_routes`` is invoked at the end of
        # the constructor, after the IAM-authorized ``/api/v1/*`` and
        # ``/inference/*`` methods are already attached — so Cognito and
        # IAM authorization coexist at the method level rather than at
        # the API level.
        self.analytics_config: AnalyticsApiConfig | None = analytics_config

        # Create the deployment-local private PKI before any client Lambda.
        # The manager writes only public trust material and ACM ARNs to SSM;
        # its KMS-encrypted root private key is inaccessible to proxy roles.
        self._create_backend_tls()

        # Create the shared backend HMAC signing key.
        self.secret = self._create_secret()

        # Global Accelerator-backed proxy routes exist only where that global
        # service is available. In other partitions the global API retains its
        # aggregate routes while callers use the regional IAM APIs directly.
        self.proxy_lambda = self._create_proxy_lambda() if self.ga_dns is not None else None
        self.inference_proxy_lambda = (
            self._create_inference_proxy_lambda() if self.ga_dns is not None else None
        )

        # Create cross-region aggregator Lambda
        self.aggregator_lambda = self._create_aggregator_lambda()

        # Create API Gateway
        self.api = self._create_api_gateway()

        # Create WAF WebACL and associate with API Gateway
        self._create_waf()

        # Export API endpoint
        self._create_outputs()

        # Wire /studio/* routes when analytics is explicitly enabled at
        # construction time. Most deployments take the mutator path
        # (:meth:`set_analytics_config`) because ``GCOAnalyticsStack`` is
        # built after this stack in ``app.py``.
        if self.analytics_config is not None:
            self._wire_studio_routes()

        # Apply cdk-nag suppressions
        self._apply_nag_suppressions()

    def _apply_nag_suppressions(self) -> None:
        """Apply cdk-nag suppressions for this stack."""
        from gco.stacks.nag_suppressions import apply_all_suppressions

        # This stack's proxy and certificate-manager roles read project-scoped
        # public SSM metadata from the global registry region. The aggregator
        # itself discovers regional bridges through CloudFormation, not SSM.
        apply_all_suppressions(
            self,
            stack_type="api_gateway",
            global_region=self.registry_region,
            project_name=self.project_name,
        )

    def _create_backend_tls(self) -> None:
        """Create the private root, regional ACM manager, schedule, and alarms."""
        config = self.backend_tls_config
        project_name = self.project_name

        self.backend_tls_key = kms.Key(
            self,
            "BackendTlsRootKey",
            alias=f"alias/{project_name}-backend-tls-root",
            description="Encrypts the GCO deployment-local backend TLS root private key",
            enable_key_rotation=True,
            removal_policy=RemovalPolicy.DESTROY,
            pending_window=Duration.days(7),
        )
        self.backend_tls_root_secret = secretsmanager.Secret(
            self,
            "BackendTlsRootSecret",
            secret_name=backend_tls_root_secret_name(project_name),
            description=(
                "Deployment-local backend TLS root CA; private key access is restricted "
                "to the certificate manager Lambda"
            ),
            encryption_key=self.backend_tls_key,
            generate_secret_string=secretsmanager.SecretStringGenerator(
                secret_string_template=json.dumps({"state": "UNINITIALIZED"}),
                generate_string_key="bootstrap_nonce",
                exclude_punctuation=True,
                password_length=32,
            ),
            removal_policy=RemovalPolicy.DESTROY,
        )

        manager_role = iam.Role(
            self,
            "BackendTlsManagerRole",
            assumed_by=iam.ServicePrincipal("lambda.amazonaws.com"),
            managed_policies=[
                iam.ManagedPolicy.from_aws_managed_policy_name(
                    "service-role/AWSLambdaBasicExecutionRole"
                )
            ],
        )
        self.backend_tls_root_secret.grant_read(manager_role)
        self.backend_tls_root_secret.grant_write(manager_role)
        self.backend_tls_key.grant_encrypt_decrypt(manager_role)
        manager_role.add_to_policy(
            iam.PolicyStatement(
                actions=[
                    "acm:AddTagsToCertificate",
                    "acm:ImportCertificate",
                    "acm:ListCertificates",
                ],
                resources=["*"],
            )
        )
        manager_role.add_to_policy(
            iam.PolicyStatement(
                actions=[
                    "acm:DeleteCertificate",
                    "acm:DescribeCertificate",
                    "acm:GetCertificate",
                    "acm:ListTagsForCertificate",
                ],
                resources=[f"arn:{self.partition}:acm:*:{self.account}:certificate/*"],
            )
        )
        manager_role.add_to_policy(
            iam.PolicyStatement(
                actions=[
                    "ssm:DeleteParameter",
                    "ssm:GetParameter",
                    "ssm:GetParametersByPath",
                    "ssm:PutParameter",
                ],
                resources=[
                    f"arn:{self.partition}:ssm:{self.registry_region}:{self.account}:"
                    f"parameter/{project_name}/backend-tls/*"
                ],
            )
        )
        manager_role.add_to_policy(
            iam.PolicyStatement(
                actions=["cloudwatch:PutMetricData"],
                resources=["*"],
                conditions={"StringEquals": {"cloudwatch:namespace": "GCO/BackendTLS"}},
            )
        )

        manager_log_group = logs.LogGroup(
            self,
            "BackendTlsManagerLogGroup",
            retention=logs.RetentionDays.ONE_MONTH,
            removal_policy=RemovalPolicy.DESTROY,
        )
        manager_environment = {
            "ROOT_SECRET_ARN": self.backend_tls_root_secret.secret_arn,
            "AWS_PARTITION": self.partition,
            "AWS_ACCOUNT_ID": self.account,
            "PROJECT_NAME": project_name,
            "REGISTRY_REGION": self.registry_region,
            "CERTIFICATE_REGIONS": json.dumps(self.certificate_regions),
            "BACKEND_TLS_SERVER_NAME": self.backend_tls_server_name,
            "ROOT_CA_PARAMETER_NAME": self.backend_tls_root_ca_parameter_name,
            "CERTIFICATE_PARAMETER_PREFIX": self.backend_tls_certificate_parameter_prefix,
            "ROOT_GENERATION": str(config["root_generation"]),
            "ROOT_VALIDITY_DAYS": str(config["root_validity_days"]),
            "ROOT_ROTATE_BEFORE_DAYS": str(config["root_rotate_before_days"]),
            "ROOT_ACTIVATION_DELAY_HOURS": str(config["root_activation_delay_hours"]),
            "ROOT_OVERLAP_DAYS": str(config["root_overlap_days"]),
            "LEAF_VALIDITY_DAYS": str(config["leaf_validity_days"]),
            "LEAF_ROTATE_BEFORE_DAYS": str(config["leaf_rotate_before_days"]),
        }
        self.backend_tls_manager_lambda = lambda_.DockerImageFunction(
            self,
            "BackendTlsCertificateManager",
            function_name=f"{project_name}-backend-tls-manager",
            code=lambda_.DockerImageCode.from_image_asset(
                directory="lambda/tls-certificate-manager",
                platform=ecr_assets.Platform.LINUX_AMD64,
            ),
            architecture=lambda_.Architecture.X86_64,
            timeout=Duration.minutes(5),
            memory_size=512,
            reserved_concurrent_executions=1,
            role=manager_role,
            environment=manager_environment,
            log_group=manager_log_group,
            tracing=lambda_.Tracing.ACTIVE,
            description="Bootstraps and rotates GCO private-root regional ACM certificates",
        )

        provider_log_group = logs.LogGroup(
            self,
            "BackendTlsProviderLogGroup",
            retention=logs.RetentionDays.ONE_MONTH,
            removal_policy=RemovalPolicy.DESTROY,
        )
        provider = cr.Provider(
            self,
            "BackendTlsProvider",
            on_event_handler=self.backend_tls_manager_lambda,
            log_group=provider_log_group,
        )
        self.backend_tls_resource = CustomResource(
            self,
            "BackendTlsCertificates",
            service_token=provider.service_token,
            properties={
                "ProjectName": project_name,
                "RegistryRegion": self.registry_region,
                "Regions": list(self.certificate_regions),
                "ServerName": self.backend_tls_server_name,
                "RootCaParameterName": self.backend_tls_root_ca_parameter_name,
                "CertificateParameterPrefix": self.backend_tls_certificate_parameter_prefix,
                "RootGeneration": config["root_generation"],
                "RootValidityDays": config["root_validity_days"],
                "RootRotateBeforeDays": config["root_rotate_before_days"],
                "RootActivationDelayHours": config["root_activation_delay_hours"],
                "RootOverlapDays": config["root_overlap_days"],
                "LeafValidityDays": config["leaf_validity_days"],
                "LeafRotateBeforeDays": config["leaf_rotate_before_days"],
                "PolicyVersion": "1",
            },
        )
        self.backend_tls_resource.node.add_dependency(self.backend_tls_root_secret)

        self.backend_tls_rotation_dlq = sqs.Queue(
            self,
            "BackendTlsRotationDlq",
            queue_name=f"{project_name}-backend-tls-rotation-dlq",
            retention_period=Duration.days(14),
            encryption=sqs.QueueEncryption.SQS_MANAGED,
            enforce_ssl=True,
            removal_policy=RemovalPolicy.DESTROY,
        )
        rotation_rule = events.Rule(
            self,
            "BackendTlsRotationSchedule",
            description="Reconcile GCO private roots and imported regional ACM leaves",
            schedule=events.Schedule.rate(Duration.hours(config["rotation_schedule_hours"])),
        )
        rotation_rule.add_target(
            events_targets.LambdaFunction(
                self.backend_tls_manager_lambda,
                event=events.RuleTargetInput.from_object({"Action": "Rotate"}),
                dead_letter_queue=self.backend_tls_rotation_dlq,
                retry_attempts=2,
                max_event_age=Duration.hours(6),
            )
        )
        rotation_rule.node.add_dependency(self.backend_tls_resource)

        self.backend_tls_manager_error_alarm = cloudwatch.Alarm(
            self,
            "BackendTlsManagerErrorAlarm",
            alarm_description="Backend TLS certificate bootstrap or rotation failed",
            metric=self.backend_tls_manager_lambda.metric_errors(
                period=Duration.minutes(15), statistic="Sum"
            ),
            threshold=1,
            evaluation_periods=1,
            comparison_operator=cloudwatch.ComparisonOperator.GREATER_THAN_OR_EQUAL_TO_THRESHOLD,
            treat_missing_data=cloudwatch.TreatMissingData.NOT_BREACHING,
        )
        self.backend_tls_rotation_dlq_alarm = cloudwatch.Alarm(
            self,
            "BackendTlsRotationDlqAlarm",
            alarm_description="Backend TLS scheduled rotation exhausted its retries",
            metric=self.backend_tls_rotation_dlq.metric_approximate_number_of_messages_visible(
                period=Duration.minutes(5)
            ),
            threshold=1,
            evaluation_periods=1,
            comparison_operator=cloudwatch.ComparisonOperator.GREATER_THAN_OR_EQUAL_TO_THRESHOLD,
            treat_missing_data=cloudwatch.TreatMissingData.NOT_BREACHING,
        )
        self.backend_tls_reconciliation_heartbeat_alarm = cloudwatch.Alarm(
            self,
            "BackendTlsReconciliationHeartbeatAlarm",
            alarm_description=(
                "Backend TLS reconciliation has not completed within two schedule intervals"
            ),
            metric=cloudwatch.Metric(
                namespace="GCO/BackendTLS",
                metric_name="ReconciliationSuccess",
                dimensions_map={"Project": project_name},
                statistic="Sum",
                period=Duration.hours(config["rotation_schedule_hours"] * 2),
            ),
            threshold=1,
            evaluation_periods=1,
            comparison_operator=cloudwatch.ComparisonOperator.LESS_THAN_THRESHOLD,
            treat_missing_data=cloudwatch.TreatMissingData.BREACHING,
        )
        self.backend_tls_root_expiry_alarm = cloudwatch.Alarm(
            self,
            "BackendTlsRootExpiryAlarm",
            alarm_description=(
                "Backend TLS root certificate is near expiry after its rotation window"
            ),
            metric=cloudwatch.Metric(
                namespace="GCO/BackendTLS",
                metric_name="RootCertificateDaysToExpiry",
                dimensions_map={"Project": project_name},
                statistic="Minimum",
                period=Duration.hours(12),
            ),
            threshold=max(1, config["root_rotate_before_days"] // 2),
            evaluation_periods=2,
            comparison_operator=cloudwatch.ComparisonOperator.LESS_THAN_THRESHOLD,
            treat_missing_data=cloudwatch.TreatMissingData.NOT_BREACHING,
        )
        self.backend_tls_expiry_alarms: list[cloudwatch.Alarm] = []
        expiry_alarm_threshold = max(1, config["leaf_rotate_before_days"] // 2)
        for region in self.certificate_regions:
            region_id = region.replace("-", "").title()
            alarm = cloudwatch.Alarm(
                self,
                f"BackendTlsLeafExpiryAlarm{region_id}",
                alarm_description=(
                    f"Backend TLS certificate in {region} is near expiry after rotation window"
                ),
                metric=cloudwatch.Metric(
                    namespace="GCO/BackendTLS",
                    metric_name="LeafCertificateDaysToExpiry",
                    dimensions_map={"Project": project_name, "Region": region},
                    statistic="Minimum",
                    period=Duration.hours(12),
                ),
                threshold=expiry_alarm_threshold,
                evaluation_periods=2,
                comparison_operator=cloudwatch.ComparisonOperator.LESS_THAN_THRESHOLD,
                treat_missing_data=cloudwatch.TreatMissingData.NOT_BREACHING,
            )
            self.backend_tls_expiry_alarms.append(alarm)

        from gco.stacks.nag_suppressions import acknowledge_nag_findings

        acknowledge_nag_findings(
            manager_role,
            [
                {
                    "id": "AwsSolutions-IAM5",
                    "reason": (
                        "ACM ImportCertificate requires Resource: * when creating a new imported "
                        "certificate because its ARN does not exist yet. Other ACM actions are "
                        "scoped to this account's certificate ARNs; SSM is scoped to the exact "
                        "project backend-tls namespace."
                    ),
                    "appliesTo": [
                        "Resource::*",
                        "Action::kms:GenerateDataKey*",
                        "Action::kms:ReEncrypt*",
                        "Resource::arn:<AWS::Partition>:acm:*:<AWS::AccountId>:certificate/*",
                        (
                            f"Resource::arn:<AWS::Partition>:ssm:{self.registry_region}:"
                            f"<AWS::AccountId>:parameter/{project_name}/backend-tls/*"
                        ),
                    ],
                },
            ],
        )
        acknowledge_nag_findings(
            provider,
            [
                {
                    "id": "AwsSolutions-IAM5",
                    "reason": (
                        "The CDK custom-resource provider invokes only versioned aliases of "
                        "BackendTlsCertificateManager; the generated :* qualifier cannot be "
                        "narrowed to a version that does not exist until deployment."
                    ),
                    "appliesTo": [
                        "Resource::<BackendTlsCertificateManager7EB9FC32.Arn>:*",
                    ],
                }
            ],
        )
        acknowledge_nag_findings(
            self.backend_tls_root_secret,
            [
                {
                    "id": "AwsSolutions-SMG4",
                    "reason": (
                        "The long-lived private root is rotated by the serialized EventBridge "
                        "certificate manager using a pending-root trust phase and overlap window; "
                        "Secrets Manager's single-value rotation protocol cannot provide that "
                        "multi-region certificate choreography."
                    ),
                },
                {
                    "id": "HIPAA.Security-SecretsManagerRotationEnabled",
                    "reason": "The EventBridge certificate manager performs staged root rotation.",
                },
                {
                    "id": "NIST.800.53.R5-SecretsManagerRotationEnabled",
                    "reason": "The EventBridge certificate manager performs staged root rotation.",
                },
            ],
        )
        acknowledge_nag_findings(
            self.backend_tls_rotation_dlq,
            [
                {
                    "id": "AwsSolutions-SQS3",
                    "reason": (
                        "This is itself EventBridge's terminal dead-letter queue; it is "
                        "retained for 14 days and monitored by BackendTlsRotationDlqAlarm. "
                        "Chaining another DLQ would only move the same terminal failure."
                    ),
                },
                {
                    "id": "Serverless-SQSRedrivePolicy",
                    "reason": (
                        "This queue is the terminal EventBridge dead-letter queue and is monitored "
                        "by BackendTlsRotationDlqAlarm; redriving it into another queue would only "
                        "move the terminal failure."
                    ),
                },
            ],
        )
        for alarm in [
            self.backend_tls_manager_error_alarm,
            self.backend_tls_rotation_dlq_alarm,
            self.backend_tls_reconciliation_heartbeat_alarm,
            self.backend_tls_root_expiry_alarm,
            *self.backend_tls_expiry_alarms,
        ]:
            acknowledge_nag_findings(
                alarm,
                [
                    {
                        "id": "HIPAA.Security-CloudWatchAlarmAction",
                        "reason": (
                            "Backend TLS alarms are retained as operator-visible stack alarms; "
                            "notification routing is deployment-specific and can be attached to "
                            "the exported alarms without granting the PKI manager publish access."
                        ),
                    },
                    {
                        "id": "NIST.800.53.R5-CloudWatchAlarmAction",
                        "reason": (
                            "Backend TLS alarms are operator-visible; notification destinations "
                            "remain deployment-specific."
                        ),
                    },
                ],
            )

    def _create_secret(self) -> secretsmanager.Secret:
        """Create the backend HMAC signing key and its daily rotation."""
        secret = secretsmanager.Secret(
            self,
            "GCOAuthSecret",
            secret_name=api_gateway_auth_secret_name(self.project_name),  # nosec B106 — this is the secret path, not a password
            description="HMAC signing key for API Gateway backend requests (auto-rotated)",
            generate_secret_string=secretsmanager.SecretStringGenerator(
                secret_string_template=json.dumps({"description": "GCO backend HMAC signing key"}),
                generate_string_key="token",
                exclude_punctuation=True,
                password_length=64,
            ),
            removal_policy=RemovalPolicy.DESTROY,
        )

        # Create rotation Lambda and store as instance attribute for monitoring
        self.rotation_lambda = self._create_rotation_lambda(secret)

        # Enable automatic rotation (daily for enhanced security)
        secret.add_rotation_schedule(
            "RotationSchedule",
            automatically_after=Duration.days(1),
            rotation_lambda=self.rotation_lambda,
        )

        return secret

    def _create_rotation_lambda(self, secret: secretsmanager.Secret) -> lambda_.Function:
        """Create Lambda function for secret rotation.

        This Lambda implements the 4-step Secrets Manager rotation protocol:
        1. createSecret - Generate new random token
        2. setSecret - No-op (no external system)
        3. testSecret - Validate token structure
        4. finishSecret - Move AWSPENDING to AWSCURRENT
        """
        # Create IAM role for rotation Lambda
        rotation_role = iam.Role(
            self,
            "RotationLambdaRole",
            assumed_by=iam.ServicePrincipal("lambda.amazonaws.com"),
            managed_policies=[
                iam.ManagedPolicy.from_aws_managed_policy_name(
                    "service-role/AWSLambdaBasicExecutionRole"
                )
            ],
        )

        # Grant permissions to manage the secret
        secret.grant_read(rotation_role)
        secret.grant_write(rotation_role)

        # Additional permissions for rotation
        rotation_role.add_to_policy(
            iam.PolicyStatement(
                actions=[
                    "secretsmanager:DescribeSecret",
                    "secretsmanager:GetSecretValue",
                    "secretsmanager:PutSecretValue",
                    "secretsmanager:UpdateSecretVersionStage",
                ],
                resources=[secret.secret_arn],
            )
        )

        # Create log group for rotation Lambda
        rotation_log_group = logs.LogGroup(
            self,
            "RotationLambdaLogGroup",
            retention=logs.RetentionDays.ONE_MONTH,
            removal_policy=RemovalPolicy.DESTROY,
        )

        # Create rotation Lambda
        rotation_lambda = lambda_.Function(
            self,
            "SecretRotationFunction",
            runtime=getattr(lambda_.Runtime, LAMBDA_PYTHON_RUNTIME),
            handler="handler.lambda_handler",
            code=lambda_.Code.from_asset("lambda/secret-rotation"),
            timeout=Duration.seconds(30),
            memory_size=128,
            role=rotation_role,
            log_group=rotation_log_group,
            description="Rotates the GCO backend HMAC signing key",
            tracing=lambda_.Tracing.ACTIVE,
        )

        # Grant Secrets Manager permission to invoke the rotation Lambda
        rotation_lambda.grant_invoke(iam.ServicePrincipal("secretsmanager.amazonaws.com"))

        # cdk-nag suppression: CDK's grant methods generate Resource: * for
        # the rotation function's execution role.
        from gco.stacks.nag_suppressions import acknowledge_nag_findings

        acknowledge_nag_findings(
            rotation_role,
            [
                {
                    "id": "AwsSolutions-IAM5",
                    "reason": (
                        "The secret rotation Lambda needs secretsmanager:GetSecretValue "
                        "and PutSecretValue on the rotation secret. CDK's grant methods "
                        "generate Resource: * for the rotation function's execution role "
                        "because the secret ARN includes a random suffix not known at "
                        "synth time."
                    ),
                    "appliesTo": ["Resource::*"],
                },
            ],
        )

        return rotation_lambda

    def _create_proxy_lambda(self) -> lambda_.Function:
        """Create the authenticated Global Accelerator backend proxy Lambda."""
        if self.ga_dns is None:
            raise RuntimeError("The global proxy requires a Global Accelerator endpoint")

        # Create IAM role
        lambda_role = iam.Role(
            self,
            "ProxyLambdaRole",
            assumed_by=iam.ServicePrincipal("lambda.amazonaws.com"),
            managed_policies=[
                iam.ManagedPolicy.from_aws_managed_policy_name(
                    "service-role/AWSLambdaBasicExecutionRole"
                )
            ],
        )

        # Grant read access to secret
        self.secret.grant_read(lambda_role)

        # The global proxy reaches regional ALBs only through Global
        # Accelerator. It needs the public root bundle but never the root
        # secret, certificate private keys, regional ALB registry, or ELB APIs.
        root_ca_parameter_arn = (
            f"arn:{self.partition}:ssm:{self.registry_region}:{self.account}:"
            f"parameter/{self.backend_tls_root_ca_parameter_name.lstrip('/')}"
        )
        lambda_role.add_to_policy(
            iam.PolicyStatement(
                effect=iam.Effect.ALLOW,
                actions=["ssm:GetParameter"],
                resources=[root_ca_parameter_arn],
            )
        )

        from gco.stacks.nag_suppressions import acknowledge_nag_findings

        acknowledge_nag_findings(
            lambda_role,
            [
                {
                    "id": "AwsSolutions-IAM5",
                    "reason": (
                        "Active X-Ray tracing requires xray:PutTraceSegments and "
                        "xray:PutTelemetryRecords on Resource::* because those APIs do not "
                        "support resource-level IAM constraints."
                    ),
                    "appliesTo": ["Resource::*"],
                }
            ],
        )

        # Create log group for Lambda
        proxy_lambda_log_group = logs.LogGroup(
            self,
            "ProxyLambdaLogGroup",
            retention=logs.RetentionDays.ONE_WEEK,
            removal_policy=RemovalPolicy.DESTROY,
        )

        # Create Lambda function
        proxy_lambda = lambda_.Function(
            self,
            "ApiGatewayProxyFunction",
            runtime=getattr(lambda_.Runtime, LAMBDA_PYTHON_RUNTIME),
            handler="handler.lambda_handler",
            code=lambda_.Code.from_asset("lambda/api-gateway-proxy"),
            timeout=Duration.seconds(29),
            memory_size=256,
            role=lambda_role,
            environment={
                "GLOBAL_ACCELERATOR_ENDPOINT": self.ga_dns,
                "SECRET_ARN": self.secret.secret_arn,
                "BACKEND_TLS_SERVER_NAME": self.backend_tls_server_name,
                "BACKEND_TLS_ROOT_CA_PARAMETER": self.backend_tls_root_ca_parameter_name,
                "BACKEND_TLS_ROOT_CA_REGION": self.registry_region,
                "BACKEND_TLS_CA_CACHE_TTL_SECONDS": str(
                    self.backend_tls_config["trust_cache_ttl_seconds"]
                ),
                "BACKEND_TLS_CA_MAX_STALE_SECONDS": str(
                    self.backend_tls_config["trust_cache_max_stale_seconds"]
                ),
            },
            log_group=proxy_lambda_log_group,
            tracing=lambda_.Tracing.ACTIVE,
        )

        return proxy_lambda

    def _create_inference_proxy_lambda(self) -> lambda_.Function:
        """Create the inference-only Lambda response-streaming proxy."""
        if self.ga_dns is None:
            raise RuntimeError("The global inference proxy requires Global Accelerator")
        role = iam.Role(
            self,
            "InferenceStreamingProxyRole",
            assumed_by=iam.ServicePrincipal("lambda.amazonaws.com"),
            managed_policies=[
                iam.ManagedPolicy.from_aws_managed_policy_name(
                    "service-role/AWSLambdaBasicExecutionRole"
                )
            ],
        )
        self.secret.grant_read(role)
        root_ca_parameter_arn = (
            f"arn:{self.partition}:ssm:{self.registry_region}:{self.account}:"
            f"parameter/{self.backend_tls_root_ca_parameter_name.lstrip('/')}"
        )
        role.add_to_policy(
            iam.PolicyStatement(
                effect=iam.Effect.ALLOW,
                actions=["ssm:GetParameter"],
                resources=[root_ca_parameter_arn],
            )
        )

        log_group = logs.LogGroup(
            self,
            "InferenceStreamingProxyLogGroup",
            retention=logs.RetentionDays.ONE_WEEK,
            removal_policy=RemovalPolicy.DESTROY,
        )
        function = lambda_.Function(
            self,
            "InferenceStreamingProxyFunction",
            runtime=getattr(lambda_.Runtime, LAMBDA_NODEJS_RUNTIME),
            handler="index.handler",
            code=lambda_.Code.from_asset("lambda/inference-streaming-proxy-build"),
            timeout=Duration.minutes(15),
            memory_size=256,
            role=role,
            environment={
                "ROUTING_MODE": "global",
                "MAX_REQUEST_BODY_BYTES": str(self.max_request_body_bytes),
                "GLOBAL_ACCELERATOR_ENDPOINT": self.ga_dns,
                "SECRET_ARN": self.secret.secret_arn,
                "BACKEND_TLS_SERVER_NAME": self.backend_tls_server_name,
                "BACKEND_TLS_ROOT_CA_PARAMETER": self.backend_tls_root_ca_parameter_name,
                "BACKEND_TLS_ROOT_CA_REGION": self.registry_region,
                "BACKEND_TLS_CA_CACHE_TTL_SECONDS": str(
                    self.backend_tls_config["trust_cache_ttl_seconds"]
                ),
                "BACKEND_TLS_CA_MAX_STALE_SECONDS": str(
                    self.backend_tls_config["trust_cache_max_stale_seconds"]
                ),
            },
            log_group=log_group,
            tracing=lambda_.Tracing.ACTIVE,
            description="Streams authenticated inference responses through Global Accelerator",
        )

        from gco.stacks.nag_suppressions import acknowledge_nag_findings

        acknowledge_nag_findings(
            role,
            [
                {
                    "id": "AwsSolutions-IAM5",
                    "reason": (
                        "Active X-Ray tracing requires write APIs on Resource::*; secret and "
                        "SSM reads remain scoped to this deployment's exact resources."
                    ),
                    "appliesTo": ["Resource::*"],
                }
            ],
        )
        return function

    def _create_aggregator_lambda(self) -> lambda_.Function:
        """Create the SigV4 regional-API aggregation Lambda.

        A Lambda in the API Gateway region cannot join every regional VPC and
        therefore must not connect directly to internal ALBs. It discovers the
        deterministic regional API Gateway stacks through CloudFormation and
        invokes their account-restricted HTTPS endpoints with its execution-role
        credentials. Each regional API's VPC Lambda then performs the private
        authenticated-TLS hop to that region's ALB.
        """
        # The exact role ARN is embedded in every regional API resource policy.
        # A project-scoped physical name keeps that ARN resolvable independently
        # in every region, avoiding an impossible cross-region CloudFormation
        # export while allowing multiple project deployments per account.
        aggregator_role = iam.Role(
            self,
            "AggregatorLambdaRole",
            assumed_by=iam.ServicePrincipal("lambda.amazonaws.com"),
            role_name=cross_region_aggregator_role_name(self.project_name),
            managed_policies=[
                iam.ManagedPolicy.from_aws_managed_policy_name(
                    "service-role/AWSLambdaBasicExecutionRole"
                )
            ],
        )
        self.aggregator_role = aggregator_role

        regional_stack_arns = [
            (
                f"arn:{self.partition}:cloudformation:{region}:{self.account}:"
                f"stack/{self.project_name}-regional-api-{region}/*"
            )
            for region in self.certificate_regions
        ]
        aggregator_role.add_to_policy(
            iam.PolicyStatement(
                effect=iam.Effect.ALLOW,
                actions=["cloudformation:DescribeStacks"],
                resources=regional_stack_arns,
            )
        )
        aggregator_role.add_to_policy(
            iam.PolicyStatement(
                effect=iam.Effect.ALLOW,
                actions=["execute-api:Invoke"],
                resources=[
                    (
                        f"arn:{self.partition}:execute-api:{region}:{self.account}:"
                        f"*/*/{method}/{path}"
                    )
                    for region in self.certificate_regions
                    for method, path in AGGREGATOR_REGIONAL_API_ROUTES
                ],
            )
        )

        aggregator_log_group = logs.LogGroup(
            self,
            "AggregatorLambdaLogGroup",
            retention=logs.RetentionDays.ONE_WEEK,
            removal_policy=RemovalPolicy.DESTROY,
        )

        aggregator_lambda = lambda_.Function(
            self,
            "CrossRegionAggregatorFunction",
            runtime=getattr(lambda_.Runtime, LAMBDA_PYTHON_RUNTIME),
            handler="handler.lambda_handler",
            code=lambda_.Code.from_asset("lambda/cross-region-aggregator"),
            timeout=Duration.seconds(29),
            memory_size=512,
            role=aggregator_role,
            environment={
                "PROJECT_NAME": self.project_name,
                "TARGET_REGIONS": json.dumps(self.certificate_regions),
                "AWS_URL_SUFFIX": self.url_suffix,
            },
            log_group=aggregator_log_group,
            description="Aggregates data through SigV4-authenticated regional GCO APIs",
            tracing=lambda_.Tracing.ACTIVE,
        )

        from gco.stacks.nag_suppressions import acknowledge_nag_findings

        acknowledge_nag_findings(
            aggregator_role,
            [
                {
                    "id": "AwsSolutions-IAM5",
                    "reason": (
                        "The aggregator uses X-Ray write APIs that require Resource::*, "
                        "describes only deterministic project/region CloudFormation stack "
                        "ARNs, and invokes only this account's generated regional API IDs "
                        "under /api/v1. Regional API resource policies admit only this role "
                        "unless operators explicitly enable direct regional access."
                    ),
                    "appliesTo": [
                        "Resource::*",
                        *[
                            (
                                f"Resource::arn:<AWS::Partition>:cloudformation:{region}:"
                                f"<AWS::AccountId>:stack/{self.project_name}-regional-api-"
                                f"{region}/*"
                            )
                            for region in self.certificate_regions
                        ],
                        *[
                            (
                                f"Resource::arn:<AWS::Partition>:execute-api:{region}:"
                                f"<AWS::AccountId>:*/*/{method}/{path}"
                            )
                            for region in self.certificate_regions
                            for method, path in AGGREGATOR_REGIONAL_API_ROUTES
                        ],
                    ],
                },
            ],
        )

        return aggregator_lambda

    def _create_api_gateway(self) -> apigateway.RestApi:
        """Create API Gateway with IAM authentication."""

        # Create CloudWatch log group
        api_log_group = logs.LogGroup(
            self,
            "ApiGatewayLogs",
            log_group_name=f"/aws/apigateway/{self.project_name}-global",
            retention=logs.RetentionDays.ONE_MONTH,
            removal_policy=RemovalPolicy.DESTROY,
        )

        configured_log_level = str(self.api_gateway_config["log_level"]).upper()
        logging_levels = {
            "OFF": apigateway.MethodLoggingLevel.OFF,
            "ERROR": apigateway.MethodLoggingLevel.ERROR,
            "INFO": apigateway.MethodLoggingLevel.INFO,
        }
        if configured_log_level not in logging_levels:
            raise ValueError(
                "api_gateway.log_level must be one of OFF, ERROR, or INFO; "
                f"got {configured_log_level!r}"
            )

        # Edge-optimized API Gateway endpoints are a commercial-partition
        # capability. Regional endpoints preserve the same IAM contract in
        # partitions where the Global Accelerator data path is unavailable.
        endpoint_type = (
            apigateway.EndpointType.EDGE
            if self.ga_dns is not None
            else apigateway.EndpointType.REGIONAL
        )
        api = apigateway.RestApi(
            self,
            "GCOGlobalApi",
            rest_api_name=f"{self.project_name}-global-api",
            description="Authenticated global aggregation API for GCO",
            endpoint_types=[endpoint_type],
            deploy=True,
            deploy_options=apigateway.StageOptions(
                stage_name="prod",
                throttling_rate_limit=self.api_gateway_config["throttle_rate_limit"],
                throttling_burst_limit=self.api_gateway_config["throttle_burst_limit"],
                logging_level=logging_levels[configured_log_level],
                # Never put inference prompts/responses (or other API bodies)
                # into execution logs. Standard access logs and metrics remain.
                data_trace_enabled=False,
                metrics_enabled=self.api_gateway_config["metrics_enabled"],
                tracing_enabled=self.api_gateway_config["tracing_enabled"],
                access_log_destination=apigateway.LogGroupLogDestination(api_log_group),
                access_log_format=apigateway.AccessLogFormat.json_with_standard_fields(
                    caller=True,
                    http_method=True,
                    ip=True,
                    protocol=True,
                    request_time=True,
                    resource_path=True,
                    response_length=True,
                    status=True,
                    user=True,
                ),
            ),
            cloud_watch_role=True,
        )

        # Add resource policy to restrict to account
        api.add_to_resource_policy(
            iam.PolicyStatement(
                effect=iam.Effect.ALLOW,
                principals=[iam.AnyPrincipal()],
                actions=["execute-api:Invoke"],
                resources=["execute-api:/*"],
                conditions={"StringEquals": {"aws:PrincipalAccount": self.account}},
            )
        )

        # Allow Cognito-authorized requests on /studio/* paths. The Cognito
        # authorizer on the method handles authentication; the resource
        # policy just needs to not block the request before it reaches the
        # authorizer. Cognito tokens don't carry aws:PrincipalAccount so
        # the account-scoped statement above would reject them.
        api.add_to_resource_policy(
            iam.PolicyStatement(
                effect=iam.Effect.ALLOW,
                principals=[iam.AnyPrincipal()],
                actions=["execute-api:Invoke"],
                resources=["execute-api:/*/GET/studio/*"],
            )
        )

        # Create /api/v1. Aggregate routes are available in every partition;
        # the GA-backed catch-all control-plane and inference routes are added
        # only when the global data path exists.
        api_resource = api.root.add_resource("api")
        v1_resource = api_resource.add_resource("v1")

        if self.proxy_lambda is not None:
            lambda_integration = apigateway.LambdaIntegration(
                self.proxy_lambda,
                proxy=True,
                timeout=Duration.seconds(29),
            )
            proxy_resource = v1_resource.add_resource("{proxy+}")
            for method in ["GET", "POST", "PUT", "DELETE", "PATCH"]:
                proxy_resource.add_method(
                    method,
                    lambda_integration,
                    authorization_type=apigateway.AuthorizationType.IAM,
                    method_responses=[
                        apigateway.MethodResponse(status_code="200"),
                        apigateway.MethodResponse(status_code="400"),
                        apigateway.MethodResponse(status_code="403"),
                        apigateway.MethodResponse(status_code="500"),
                    ],
                )

        self._create_global_routes(api, v1_resource)

        if self.inference_proxy_lambda is not None:
            inference_integration = apigateway.LambdaIntegration(
                self.inference_proxy_lambda,
                proxy=True,
                timeout=Duration.minutes(15),
                response_transfer_mode=apigateway.ResponseTransferMode.STREAM,
            )
            self._create_inference_routes(api, inference_integration)

        return api

    def _create_global_routes(
        self, api: apigateway.RestApi, v1_resource: apigateway.Resource
    ) -> None:
        """Create routes for cross-region aggregation endpoints.

        Routes:
            GET /api/v1/global/jobs - List jobs across all regions
            DELETE /api/v1/global/jobs - Bulk delete across all regions
            GET /api/v1/global/health - Health status across all regions
            GET /api/v1/global/status - Cluster status across all regions
        """
        # Create Lambda integration for aggregator
        aggregator_integration = apigateway.LambdaIntegration(
            self.aggregator_lambda, proxy=True, timeout=Duration.seconds(29)
        )

        # Create /global resource
        global_resource = v1_resource.add_resource("global")

        # /global/jobs
        global_jobs = global_resource.add_resource("jobs")
        for method in ["GET", "DELETE"]:
            global_jobs.add_method(
                method,
                aggregator_integration,
                authorization_type=apigateway.AuthorizationType.IAM,
                method_responses=[
                    apigateway.MethodResponse(status_code="200"),
                    apigateway.MethodResponse(status_code="400"),
                    apigateway.MethodResponse(status_code="500"),
                ],
            )

        # /global/health
        global_health = global_resource.add_resource("health")
        global_health.add_method(
            "GET",
            aggregator_integration,
            authorization_type=apigateway.AuthorizationType.IAM,
            method_responses=[
                apigateway.MethodResponse(status_code="200"),
                apigateway.MethodResponse(status_code="500"),
            ],
        )

        # /global/status
        global_status = global_resource.add_resource("status")
        global_status.add_method(
            "GET",
            aggregator_integration,
            authorization_type=apigateway.AuthorizationType.IAM,
            method_responses=[
                apigateway.MethodResponse(status_code="200"),
                apigateway.MethodResponse(status_code="500"),
            ],
        )

    def _create_inference_routes(
        self,
        api: apigateway.RestApi,
        lambda_integration: apigateway.LambdaIntegration,
    ) -> None:
        """Create proxy route for inference endpoints.

        Routes:
            GET|HEAD|POST /inference/{proxy+} → streaming Lambda → GA → ALB → inference proxy

        The dedicated in-cluster service enforces endpoint state and the serving-
        path allowlist before opening a streaming connection to a model server.
        """
        inference_resource = api.root.add_resource("inference")
        inference_proxy = inference_resource.add_resource("{proxy+}")

        for method in ["GET", "HEAD", "POST"]:
            inference_proxy.add_method(
                method,
                lambda_integration,
                authorization_type=apigateway.AuthorizationType.IAM,
                method_responses=[
                    apigateway.MethodResponse(status_code="200"),
                    apigateway.MethodResponse(status_code="400"),
                    apigateway.MethodResponse(status_code="404"),
                    apigateway.MethodResponse(status_code="500"),
                    apigateway.MethodResponse(status_code="502"),
                ],
            )

    def _create_outputs(self) -> None:
        """Export API Gateway endpoint."""

        CfnOutput(
            self,
            "ApiEndpoint",
            value=self.api.url,
            description="Global API Gateway endpoint (IAM authenticated)",
            export_name=f"{self.project_name}-global-api-endpoint",
        )

        CfnOutput(
            self,
            "SecretArn",
            value=self.secret.secret_arn,
            description="Backend HMAC signing-key secret ARN",
            export_name=f"{self.project_name}-auth-secret-arn",
        )

        CfnOutput(
            self,
            "BackendTlsServerName",
            value=self.backend_tls_server_name,
            description="Private SNI identity verified on every proxy-to-ALB TLS connection",
            export_name=f"{self.project_name}-backend-tls-server-name",
        )

        CfnOutput(
            self,
            "BackendTlsRootCaParameter",
            value=self.backend_tls_root_ca_parameter_name,
            description="SSM parameter containing the public backend TLS root trust bundle",
            export_name=f"{self.project_name}-backend-tls-root-ca-parameter",
        )

    def set_analytics_config(self, config: AnalyticsApiConfig) -> None:
        """Attach a post-construction ``AnalyticsApiConfig`` and wire ``/studio/*`` routes.

        ``GCOAnalyticsStack`` is created *after* ``GCOApiGatewayGlobalStack``
        in ``app.py`` (the regional stacks already declare a dependency on
        the API gateway stack, so re-ordering the two global stacks would
        ripple through the entire stack graph). This mutator lets
        ``app.py`` defer attaching the analytics integration until after
        both stacks exist, without changing the constructor contract or
        the existing cross-stack dependency wiring.

        MUST be called **at most once**, and only before stack synthesis
        finishes. Calling it twice raises ``RuntimeError`` so the caller
        cannot accidentally double-wire the Cognito authorizer (which
        would produce two authorizers with overlapping identity sources
        on the same REST API).

        Args:
            config: The ``AnalyticsApiConfig`` built from the
                ``GCOAnalyticsStack`` attributes. Must be non-``None`` —
                pass ``None`` at construction time instead if analytics
                is disabled.

        Raises:
            RuntimeError: if the stack already has an attached
                ``analytics_config`` (from either constructor kwarg or
                a prior ``set_analytics_config`` call).
        """
        if self.analytics_config is not None:
            raise RuntimeError(
                "GCOApiGatewayGlobalStack.set_analytics_config may only be called "
                "once. The stack already has an analytics_config attached."
            )
        self.analytics_config = config
        self._wire_studio_routes()

    def _wire_studio_routes(self) -> None:
        """Attach the Cognito-authorized ``/studio/*`` route tree.

        Called from ``__init__`` when an ``AnalyticsApiConfig`` is passed
        to the constructor, or from :meth:`set_analytics_config` when the
        config is attached post-construction. Safe to skip entirely when
        analytics is disabled — the caller is responsible for gating on
        ``self.analytics_config is not None``.

        Wiring order matters: this runs *after* ``_create_api_gateway``
        has already attached the IAM-authorized ``/api/v1/*`` and
        ``/inference/*`` methods. The Cognito authorizer coexists with
        those methods at the method level (not at the REST API level),
        so the existing IAM-authorized methods are untouched — see the
        coexistence assertion in
        ``tests/test_api_gateway_analytics_config.py``.

        Resources added:

        * ``CognitoUserPoolsAuthorizer`` named ``StudioCognitoAuthorizer``
          referencing ``UserPool.from_user_pool_arn(...)``.
        * ``RequestValidator`` with ``validate_request_parameters=True``
          attached to the ``/studio/login`` method via
          ``request_validator_options``.
        * ``/studio`` + ``/studio/login`` + ``/studio/callback``
          resources.
        * ``GET /studio/login`` — Cognito-authorized,
          ``LambdaIntegration(presigned_url_lambda, proxy=True,
          timeout=Duration.seconds(29))``.
        * ``GET /studio/callback`` — unauthenticated stub MOCK
          integration returning a 200 with an empty body; serves as the
          OAuth redirect landing page when Cognito hosted UI is enabled.
        * ``CfnOutput`` ``CognitoAuthorizerId`` with the authorizer's
          ``authorizer_id``.
        * ``CfnOutput`` ``StudioLoginUrl`` — concrete
          ``https://<api-id>.execute-api.<region>.amazonaws.com/prod/studio/login``
          constructed at deploy time via ``Fn.sub`` because the REST API
          id is a deploy-time token.
        """
        assert self.analytics_config is not None, (
            "_wire_studio_routes called without an AnalyticsApiConfig attached."
        )
        analytics_config = self.analytics_config

        # Build the authorizer against the Cognito user pool that owns
        # Studio identities. ``from_user_pool_arn`` is an import — no
        # new Cognito resources are created in this stack.
        user_pool = cognito.UserPool.from_user_pool_arn(
            self,
            "StudioUserPoolRef",
            analytics_config.user_pool_arn,
        )
        authorizer = apigateway.CognitoUserPoolsAuthorizer(
            self,
            "StudioCognitoAuthorizer",
            cognito_user_pools=[user_pool],
            authorizer_name=f"{self.project_name}-studio-cognito-authorizer",
        )
        # The authorizer attaches itself to the RestApi automatically
        # the first time it is passed into ``add_method``. No explicit
        # attach call is needed (and the CDK API does not expose a
        # public one for ``CognitoUserPoolsAuthorizer``).

        # Request validator — validates query/path parameters are
        # present before the Lambda is invoked (the Cognito ID token
        # itself is validated by the authorizer, not this validator).
        studio_request_validator = apigateway.RequestValidator(
            self,
            "StudioRequestValidator",
            rest_api=self.api,
            request_validator_name=f"{self.project_name}-studio-request-validator",
            validate_request_parameters=True,
        )

        # /studio → /studio/login + /studio/callback
        studio_resource = self.api.root.add_resource("studio")
        login_resource = studio_resource.add_resource("login")
        callback_resource = studio_resource.add_resource("callback")

        # /studio/login — Cognito-authorized, proxies to the
        # presigned-URL Lambda. 29-second integration timeout matches
        # the Lambda timeout so the Lambda is the one that times out
        # on slow SageMaker API calls rather than API Gateway.
        login_integration = apigateway.LambdaIntegration(
            analytics_config.presigned_url_lambda,
            proxy=True,
            timeout=Duration.seconds(29),
        )
        login_resource.add_method(
            "GET",
            login_integration,
            authorization_type=apigateway.AuthorizationType.COGNITO,
            authorizer=authorizer,
            request_validator=studio_request_validator,
            method_responses=[
                apigateway.MethodResponse(status_code="200"),
                apigateway.MethodResponse(status_code="400"),
                apigateway.MethodResponse(status_code="401"),
                apigateway.MethodResponse(status_code="404"),
                apigateway.MethodResponse(status_code="500"),
            ],
        )

        # /studio/callback — stub 200 OK landing page for the Cognito
        # hosted UI OAuth redirect flow. Unauthenticated MOCK
        # integration so the page is reachable without a signed
        # request. The body is intentionally empty — the hosted UI
        # consumes the query-string code parameter, not the response
        # body.
        callback_integration = apigateway.MockIntegration(
            integration_responses=[
                apigateway.IntegrationResponse(
                    status_code="200",
                    response_templates={"application/json": ""},
                ),
            ],
            request_templates={"application/json": '{"statusCode": 200}'},
        )
        callback_method = callback_resource.add_method(
            "GET",
            callback_integration,
            authorization_type=apigateway.AuthorizationType.NONE,
            method_responses=[
                apigateway.MethodResponse(status_code="200"),
            ],
        )

        # /studio/callback is intentionally unauthenticated — it's the
        # Cognito hosted-UI OAuth redirect landing page where the
        # authorization ``code`` query-string parameter is consumed by
        # the client-side JavaScript. Adding IAM or Cognito authorization
        # here would break the OAuth flow because the browser redirect
        # from Cognito does not carry SigV4 or an id-token header.
        from gco.stacks.nag_suppressions import acknowledge_nag_findings

        acknowledge_nag_findings(
            callback_method,
            [
                {
                    "id": "AwsSolutions-APIG4",
                    "reason": (
                        "/studio/callback is the Cognito hosted-UI OAuth "
                        "redirect landing page. The browser redirect from "
                        "Cognito carries the authorization code as a "
                        "query-string parameter; it does NOT carry SigV4 "
                        "or an id-token header. Adding IAM or Cognito "
                        "authorization here would break the OAuth flow. "
                        "The route is a MOCK integration that returns an "
                        "empty 200 body; it does not expose any backend "
                        "resources."
                    ),
                },
            ],
        )

        # CfnOutputs — the CLI reads these for auto-discovery.
        CfnOutput(
            self,
            "CognitoAuthorizerId",
            value=authorizer.authorizer_id,
            description="API Gateway authorizer id for the Studio Cognito authorizer",
            export_name=f"{self.project_name}-studio-cognito-authorizer-id",
        )
        # ``self.api.url`` already resolves to the deploy-time URL, but
        # it points at the stage root. Use ``Fn.sub`` to append the
        # concrete ``studio/login`` suffix so operators get a copy-
        # pastable login URL in the stack outputs.
        studio_login_url = Fn.sub(
            "https://${ApiId}.execute-api.${AWS::Region}.${AWS::URLSuffix}/${Stage}/studio/login",
            {
                "ApiId": self.api.rest_api_id,
                "Stage": self.api.deployment_stage.stage_name,
            },
        )
        CfnOutput(
            self,
            "StudioLoginUrl",
            value=studio_login_url,
            description="Concrete URL for the /studio/login route (Cognito-authenticated)",
            export_name=f"{self.project_name}-studio-login-url",
        )

    def _create_waf(self) -> None:
        """Create WAF WebACL with AWS Managed Rules for API Gateway protection.

        This implements a comprehensive WAF setup using AWS Managed Rule Groups
        for protection against:
        - Common web exploits (OWASP Top 10)
        - Known bad inputs
        - SQL injection
        - Linux-specific attacks
        - IP reputation threats
        - Anonymous IP addresses (Tor, VPNs, proxies)

        The WebACL is associated with the API Gateway stage for edge protection.
        Logging is enabled to CloudWatch Logs for compliance (HIPAA, NIST, PCI-DSS).
        """
        # Create CloudWatch Log Group for WAF logs
        # WAF requires log group name to start with "aws-waf-logs-"
        waf_log_group = logs.LogGroup(
            self,
            "WafLogGroup",
            log_group_name=f"aws-waf-logs-{self.project_name}-api-gateway",
            retention=logs.RetentionDays.ONE_MONTH,
            removal_policy=RemovalPolicy.DESTROY,
        )

        # Create WAF WebACL with AWS Managed Rules
        # Note: For API Gateway (even edge-optimized), use REGIONAL scope
        # The WAF is associated with the API Gateway stage, not CloudFront directly
        #
        # Rule priority ordering:
        #   0  -> PerIPRateLimit (evaluated FIRST so abusive IPs are blocked
        #         before expensive managed rule groups run)
        #   1  -> Preserve the CRS 8 KiB body limit outside /inference/*
        #   2-7 -> AWS Managed Rule Groups
        waf_config = self.node.try_get_context("waf") or {}
        per_ip_rate_limit = int(waf_config.get("per_ip_rate_limit", 100))

        self.web_acl = wafv2.CfnWebACL(
            self,
            "GCOWebAcl",
            name=f"{self.project_name}-api-gateway-waf",
            description="WAF WebACL for GCO API Gateway with AWS Managed Rules",
            scope="REGIONAL",  # REGIONAL for API Gateway association
            default_action=wafv2.CfnWebACL.DefaultActionProperty(allow={}),
            visibility_config=wafv2.CfnWebACL.VisibilityConfigProperty(
                cloud_watch_metrics_enabled=True,
                metric_name="GCOApiGatewayWaf",
                sampled_requests_enabled=True,
            ),
            rules=[
                # Rule 0: Per-source-IP rate limiting (HIGHEST PRIORITY).
                # Evaluated before any AWS Managed Rule Group so that abusive
                # IPs are blocked immediately without consuming WCUs on the
                # heavier managed rule groups. Aggregates requests per source
                # IP over a rolling 5-minute window (AWS WAF fixed behavior
                # for rate-based statements).
                #
                # The limit is configurable via `cdk.json` context
                # `waf.per_ip_rate_limit` (default: 100 requests / 5 min).
                wafv2.CfnWebACL.RuleProperty(
                    name="PerIPRateLimit",
                    priority=0,
                    action=wafv2.CfnWebACL.RuleActionProperty(block={}),
                    statement=wafv2.CfnWebACL.StatementProperty(
                        rate_based_statement=wafv2.CfnWebACL.RateBasedStatementProperty(
                            limit=per_ip_rate_limit,
                            aggregate_key_type="IP",
                        )
                    ),
                    visibility_config=wafv2.CfnWebACL.VisibilityConfigProperty(
                        cloud_watch_metrics_enabled=True,
                        metric_name="PerIPRateLimit",
                        sampled_requests_enabled=True,
                    ),
                ),
                # Rule 1: Preserve the CRS 8 KiB body limit for every route
                # except the deployed /prod/inference/{proxy+} route. API Gateway
                # invoke URLs include the stage in the client URI that WAF inspects,
                # and the trailing slash is the route boundary: /prod/inference-extra
                # must remain subject to this limit. Inference accepts bodies up to
                # the backend's authoritative 1 MiB limit. MATCH fails closed on
                # oversized control-plane bodies beyond WAF's inspection window.
                wafv2.CfnWebACL.RuleProperty(
                    name="NonInferenceBodySizeLimit",
                    priority=1,
                    action=wafv2.CfnWebACL.RuleActionProperty(block={}),
                    statement=wafv2.CfnWebACL.StatementProperty(
                        and_statement=wafv2.CfnWebACL.AndStatementProperty(
                            statements=[
                                wafv2.CfnWebACL.StatementProperty(
                                    size_constraint_statement=wafv2.CfnWebACL.SizeConstraintStatementProperty(
                                        comparison_operator="GT",
                                        field_to_match=wafv2.CfnWebACL.FieldToMatchProperty(
                                            body=wafv2.CfnWebACL.BodyProperty(
                                                oversize_handling="MATCH"
                                            )
                                        ),
                                        size=8_192,
                                        text_transformations=[
                                            wafv2.CfnWebACL.TextTransformationProperty(
                                                priority=0,
                                                type="NONE",
                                            )
                                        ],
                                    )
                                ),
                                wafv2.CfnWebACL.StatementProperty(
                                    not_statement=wafv2.CfnWebACL.NotStatementProperty(
                                        statement=wafv2.CfnWebACL.StatementProperty(
                                            byte_match_statement=wafv2.CfnWebACL.ByteMatchStatementProperty(
                                                field_to_match=wafv2.CfnWebACL.FieldToMatchProperty(
                                                    uri_path={}
                                                ),
                                                positional_constraint="STARTS_WITH",
                                                search_string="/prod/inference/",
                                                text_transformations=[
                                                    wafv2.CfnWebACL.TextTransformationProperty(
                                                        priority=0,
                                                        type="NONE",
                                                    )
                                                ],
                                            )
                                        )
                                    )
                                ),
                            ]
                        )
                    ),
                    visibility_config=wafv2.CfnWebACL.VisibilityConfigProperty(
                        cloud_watch_metrics_enabled=True,
                        metric_name="NonInferenceBodySizeLimit",
                        sampled_requests_enabled=True,
                    ),
                ),
                # Rule 2: AWS Managed Rules - Common Rule Set (OWASP Top 10).
                # Override only SizeRestrictions_BODY: every other CRS rule
                # continues to block normally, including body-content rules.
                wafv2.CfnWebACL.RuleProperty(
                    name="AWSManagedRulesCommonRuleSet",
                    priority=2,
                    override_action=wafv2.CfnWebACL.OverrideActionProperty(none={}),
                    statement=wafv2.CfnWebACL.StatementProperty(
                        managed_rule_group_statement=wafv2.CfnWebACL.ManagedRuleGroupStatementProperty(
                            vendor_name="AWS",
                            name="AWSManagedRulesCommonRuleSet",
                            rule_action_overrides=[
                                wafv2.CfnWebACL.RuleActionOverrideProperty(
                                    name="SizeRestrictions_BODY",
                                    action_to_use=wafv2.CfnWebACL.RuleActionProperty(count={}),
                                )
                            ],
                        )
                    ),
                    visibility_config=wafv2.CfnWebACL.VisibilityConfigProperty(
                        cloud_watch_metrics_enabled=True,
                        metric_name="AWSManagedRulesCommonRuleSet",
                        sampled_requests_enabled=True,
                    ),
                ),
                # Rule 3: AWS Managed Rules - Known Bad Inputs
                wafv2.CfnWebACL.RuleProperty(
                    name="AWSManagedRulesKnownBadInputsRuleSet",
                    priority=3,
                    override_action=wafv2.CfnWebACL.OverrideActionProperty(none={}),
                    statement=wafv2.CfnWebACL.StatementProperty(
                        managed_rule_group_statement=wafv2.CfnWebACL.ManagedRuleGroupStatementProperty(
                            vendor_name="AWS",
                            name="AWSManagedRulesKnownBadInputsRuleSet",
                        )
                    ),
                    visibility_config=wafv2.CfnWebACL.VisibilityConfigProperty(
                        cloud_watch_metrics_enabled=True,
                        metric_name="AWSManagedRulesKnownBadInputsRuleSet",
                        sampled_requests_enabled=True,
                    ),
                ),
                # Rule 4: AWS Managed Rules - SQL Injection
                wafv2.CfnWebACL.RuleProperty(
                    name="AWSManagedRulesSQLiRuleSet",
                    priority=4,
                    override_action=wafv2.CfnWebACL.OverrideActionProperty(none={}),
                    statement=wafv2.CfnWebACL.StatementProperty(
                        managed_rule_group_statement=wafv2.CfnWebACL.ManagedRuleGroupStatementProperty(
                            vendor_name="AWS",
                            name="AWSManagedRulesSQLiRuleSet",
                        )
                    ),
                    visibility_config=wafv2.CfnWebACL.VisibilityConfigProperty(
                        cloud_watch_metrics_enabled=True,
                        metric_name="AWSManagedRulesSQLiRuleSet",
                        sampled_requests_enabled=True,
                    ),
                ),
                # Rule 5: AWS Managed Rules - Linux OS (protects against Linux-specific attacks)
                wafv2.CfnWebACL.RuleProperty(
                    name="AWSManagedRulesLinuxRuleSet",
                    priority=5,
                    override_action=wafv2.CfnWebACL.OverrideActionProperty(none={}),
                    statement=wafv2.CfnWebACL.StatementProperty(
                        managed_rule_group_statement=wafv2.CfnWebACL.ManagedRuleGroupStatementProperty(
                            vendor_name="AWS",
                            name="AWSManagedRulesLinuxRuleSet",
                        )
                    ),
                    visibility_config=wafv2.CfnWebACL.VisibilityConfigProperty(
                        cloud_watch_metrics_enabled=True,
                        metric_name="AWSManagedRulesLinuxRuleSet",
                        sampled_requests_enabled=True,
                    ),
                ),
                # Rule 6: AWS Managed Rules - Amazon IP Reputation List
                wafv2.CfnWebACL.RuleProperty(
                    name="AWSManagedRulesAmazonIpReputationList",
                    priority=6,
                    override_action=wafv2.CfnWebACL.OverrideActionProperty(none={}),
                    statement=wafv2.CfnWebACL.StatementProperty(
                        managed_rule_group_statement=wafv2.CfnWebACL.ManagedRuleGroupStatementProperty(
                            vendor_name="AWS",
                            name="AWSManagedRulesAmazonIpReputationList",
                        )
                    ),
                    visibility_config=wafv2.CfnWebACL.VisibilityConfigProperty(
                        cloud_watch_metrics_enabled=True,
                        metric_name="AWSManagedRulesAmazonIpReputationList",
                        sampled_requests_enabled=True,
                    ),
                ),
                # Rule 7: AWS Managed Rules - Anonymous IP List (blocks Tor, VPNs, proxies)
                wafv2.CfnWebACL.RuleProperty(
                    name="AWSManagedRulesAnonymousIpList",
                    priority=7,
                    override_action=wafv2.CfnWebACL.OverrideActionProperty(none={}),
                    statement=wafv2.CfnWebACL.StatementProperty(
                        managed_rule_group_statement=wafv2.CfnWebACL.ManagedRuleGroupStatementProperty(
                            vendor_name="AWS",
                            name="AWSManagedRulesAnonymousIpList",
                        )
                    ),
                    visibility_config=wafv2.CfnWebACL.VisibilityConfigProperty(
                        cloud_watch_metrics_enabled=True,
                        metric_name="AWSManagedRulesAnonymousIpList",
                        sampled_requests_enabled=True,
                    ),
                ),
            ],
        )

        # Enable WAF logging to CloudWatch Logs
        # This is required for HIPAA, NIST 800-53, and PCI-DSS compliance
        wafv2.CfnLoggingConfiguration(
            self,
            "WafLoggingConfig",
            resource_arn=self.web_acl.attr_arn,
            log_destination_configs=[waf_log_group.log_group_arn],
        )

        # Associate WAF WebACL with API Gateway stage
        # For API Gateway, use the stage ARN format
        wafv2.CfnWebACLAssociation(
            self,
            "GCOWebAclAssociation",
            resource_arn=self.api.deployment_stage.stage_arn,
            web_acl_arn=self.web_acl.attr_arn,
        )

        # Output WAF WebACL ARN
        CfnOutput(
            self,
            "WebAclArn",
            value=self.web_acl.attr_arn,
            description="WAF WebACL ARN for API Gateway protection",
            export_name=f"{self.project_name}-waf-webacl-arn",
        )
