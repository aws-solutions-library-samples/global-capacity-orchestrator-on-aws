"""
Regional API Gateway bridge for authenticated access to private regional ALBs.

Every deployment creates this regional bridge so the centralized aggregator has
a reachable, IAM-authenticated path into each regional VPC. In the commercial
``aws`` partition, ``api_gateway.regional_api_enabled`` optionally admits other
same-account principals. In every other AWS partition, Global Accelerator is
omitted and this regional IAM path is enabled for same-account callers
regardless of that setting.

Architecture:
    Aggregator → Regional API Gateway → buffered VPC Lambda → Internal ALB → EKS pods
    User (optional in ``aws``; required elsewhere) ────────┤
                                        └→ streaming VPC Lambda → inference proxy

Security:
    - API Gateway uses AWS-managed TLS and IAM authentication (SigV4)
    - The resource policy always admits only the aggregator role by default
    - Optional direct mode additionally admits IAM-authorized account principals
    - Lambda runs inside the VPC with access to the internal ALB
    - Lambda verifies the deployment-local ALB certificate with explicit SNI
    - Lambda adds a short-lived per-request HMAC envelope to the ALB request
    - No public exposure of the ALB or EKS API

Configuration:
    In the commercial ``aws`` partition, set
    ``api_gateway.regional_api_enabled`` to ``true`` when callers need direct
    region-pinned access. Outside that partition, the regional API is the
    supported workload ingress and same-account access is forced on. Global
    aggregation always uses its dedicated role in every partition.
"""

from typing import Any

from aws_cdk import (
    CfnOutput,
    Duration,
    RemovalPolicy,
    Stack,
)
from aws_cdk import aws_apigateway as apigateway
from aws_cdk import aws_ec2 as ec2
from aws_cdk import aws_iam as iam
from aws_cdk import aws_lambda as lambda_
from aws_cdk import aws_logs as logs
from constructs import Construct

from gco.config.config_loader import ConfigLoader
from gco.stacks.constants import (
    AGGREGATOR_REGIONAL_API_ROUTES,
    DEFAULT_MAX_REQUEST_BODY_BYTES,
    LAMBDA_NODEJS_RUNTIME,
    LAMBDA_PYTHON_RUNTIME,
    backend_tls_root_ca_parameter_name,
    backend_tls_server_name,
    validated_request_body_limit,
)

# <pyflowchart-code-diagram> BEGIN - auto-inserted, do not edit
# Generated at (UTC): 2026-07-18T01:03:40Z
# Flowchart(s) generated from this file:
#   * ``GCORegionalApiGatewayStack.__init__`` -> ``diagrams/code_diagrams/gco/stacks/regional_api_gateway_stack.GCORegionalApiGatewayStack___init__.html``
#     (PNG: ``diagrams/code_diagrams/gco/stacks/regional_api_gateway_stack.GCORegionalApiGatewayStack___init__.png``)
# Regenerate with ``python diagrams/code_diagrams/generate.py``.
# <pyflowchart-code-diagram> END


class GCORegionalApiGatewayStack(Stack):
    """Regional aggregation bridge with optional direct caller access.

    The VPC Lambda gives the global aggregator a reachable path to one internal
    regional ALB. Direct region-pinned access for other IAM-authorized account
    principals is optional in the commercial ``aws`` partition and mandatory
    in partitions where Global Accelerator is unavailable.

    Attributes:
        api: Regional REST API with IAM authentication.
        proxy_lambda: Buffered VPC Lambda for ``/api/v1/*`` requests.
        inference_proxy_lambda: Response-streaming VPC Lambda for ``/inference/*``.
    """

    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        config: ConfigLoader,
        region: str,
        vpc: ec2.IVpc,
        auth_secret_arn: str,
        aggregator_role_arn: str,
        alb_dns_name: str | None = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)

        self.config = config
        self.deployment_region = region
        self.vpc = vpc
        self.alb_dns_name = alb_dns_name
        supports_global_accelerator = getattr(config, "supports_global_accelerator", None)
        self.global_accelerator_enabled = (
            bool(supports_global_accelerator()) if callable(supports_global_accelerator) else True
        )
        self.auth_secret_arn = auth_secret_arn
        self.aggregator_role_arn = aggregator_role_arn

        # Keep control-plane calls on the established buffered Python proxy and
        # give inference a separate Node.js response-streaming runtime.
        self.proxy_lambda = self._create_vpc_proxy_lambda()
        self.inference_proxy_lambda = self._create_inference_proxy_lambda()

        # Create regional API Gateway
        self.api = self._create_api_gateway()

        # Export outputs
        self._create_outputs()

        # Apply cdk-nag suppressions
        self._apply_nag_suppressions()

    def _apply_nag_suppressions(self) -> None:
        """Apply cdk-nag suppressions for this stack."""
        from gco.stacks.nag_suppressions import apply_all_suppressions

        apply_all_suppressions(
            self,
            stack_type="regional_api_gateway",
            global_region=self.config.get_global_region(),
            project_name=self.config.get_project_name(),
        )

    def _create_vpc_proxy_lambda(self) -> lambda_.Function:
        """Create VPC Lambda that proxies requests to internal ALB."""
        project_name = self.config.get_project_name()
        backend_tls_config = self.config.get_backend_tls_config()
        root_ca_parameter_name = backend_tls_root_ca_parameter_name(project_name)

        # Create security group for Lambda
        lambda_sg = ec2.SecurityGroup(
            self,
            "ProxyLambdaSg",
            vpc=self.vpc,
            description="Security group for regional API proxy Lambdas",
            allow_all_outbound=True,
        )
        self._proxy_lambda_security_group = lambda_sg

        # Create IAM role for Lambda
        # role_name intentionally omitted - let CDK generate unique name
        lambda_role = iam.Role(
            self,
            "ProxyLambdaRole",
            assumed_by=iam.ServicePrincipal("lambda.amazonaws.com"),
            managed_policies=[
                iam.ManagedPolicy.from_aws_managed_policy_name(
                    "service-role/AWSLambdaVPCAccessExecutionRole"
                )
            ],
        )

        # Grant read access to auth secret.
        lambda_role.add_to_policy(
            iam.PolicyStatement(
                effect=iam.Effect.ALLOW,
                actions=[
                    "secretsmanager:GetSecretValue",
                    "secretsmanager:DescribeSecret",
                ],
                resources=[f"{self.auth_secret_arn}*"],
            )
        )

        # The Ingress-created ALB does not exist during CDK synthesis. Resolve
        # its current hostname from the project-scoped SSM registry at request
        # time, then verify that the hostname belongs to this account, region,
        # EKS cluster, and platform Ingress before forwarding any request.
        registry_region = self.config.get_global_region()
        registry_parameter_arn = (
            f"arn:{self.partition}:ssm:{registry_region}:{self.account}:"
            f"parameter/{project_name}/alb-hostname-{self.deployment_region}"
        )
        root_ca_parameter_arn = (
            f"arn:{self.partition}:ssm:{registry_region}:{self.account}:"
            f"parameter/{root_ca_parameter_name.lstrip('/')}"
        )
        lambda_role.add_to_policy(
            iam.PolicyStatement(
                effect=iam.Effect.ALLOW,
                actions=["ssm:GetParameter"],
                resources=[registry_parameter_arn, root_ca_parameter_arn],
            )
        )
        lambda_role.add_to_policy(
            iam.PolicyStatement(
                effect=iam.Effect.ALLOW,
                actions=[
                    "elasticloadbalancing:DescribeLoadBalancers",
                    "elasticloadbalancing:DescribeTags",
                ],
                resources=["*"],
            )
        )

        from gco.stacks.nag_suppressions import acknowledge_nag_findings

        acknowledge_nag_findings(
            lambda_role,
            [
                {
                    "id": "AwsSolutions-IAM5",
                    "reason": (
                        "ELB DescribeLoadBalancers and DescribeTags do not support "
                        "resource-level scoping. They are read-only and are used only "
                        "to verify that the SSM-registered hostname belongs to this "
                        "account's exact regional GCO cluster and platform Ingress."
                    ),
                    "appliesTo": ["Resource::*"],
                }
            ],
        )

        # Create log group
        # log_group_name intentionally omitted - let CDK generate unique name
        log_group = logs.LogGroup(
            self,
            "ProxyLambdaLogGroup",
            retention=logs.RetentionDays.ONE_WEEK,
            removal_policy=RemovalPolicy.DESTROY,
        )

        # A literal endpoint remains available for isolated stack synthesis and
        # compatibility callers. Production app wiring omits it so replacements
        # are discovered from SSM without requiring an ALB at deploy time.
        environment = {
            "SECRET_ARN": self.auth_secret_arn,
            "REGISTRY_REGION": registry_region,
            "TARGET_REGION": self.deployment_region,
            "PROJECT_NAME": project_name,
            "AWS_ACCOUNT_ID": self.account,
            "AWS_URL_SUFFIX": self.url_suffix,
            "BACKEND_TLS_SERVER_NAME": backend_tls_server_name(project_name),
            "BACKEND_TLS_ROOT_CA_PARAMETER": root_ca_parameter_name,
            "BACKEND_TLS_ROOT_CA_REGION": registry_region,
            "BACKEND_TLS_CA_CACHE_TTL_SECONDS": str(backend_tls_config["trust_cache_ttl_seconds"]),
            "BACKEND_TLS_CA_MAX_STALE_SECONDS": str(
                backend_tls_config["trust_cache_max_stale_seconds"]
            ),
        }
        if self.alb_dns_name:
            environment["ALB_ENDPOINT"] = self.alb_dns_name

        # Create Lambda function in VPC
        proxy_lambda = lambda_.Function(
            self,
            "RegionalProxyFunction",
            function_name=f"{project_name}-regional-proxy-{self.deployment_region}",
            runtime=getattr(lambda_.Runtime, LAMBDA_PYTHON_RUNTIME),
            handler="handler.lambda_handler",
            code=lambda_.Code.from_asset("lambda/regional-api-proxy"),
            timeout=Duration.seconds(29),
            memory_size=256,
            role=lambda_role,
            vpc=self.vpc,
            vpc_subnets=ec2.SubnetSelection(subnet_type=ec2.SubnetType.PRIVATE_WITH_EGRESS),
            security_groups=[lambda_sg],
            environment=environment,
            log_group=log_group,
            description=f"Regional API proxy for {self.deployment_region} (VPC Lambda)",
            tracing=lambda_.Tracing.ACTIVE,
        )

        return proxy_lambda

    def _create_inference_proxy_lambda(self) -> lambda_.Function:
        """Create the VPC Lambda that streams inference responses from the ALB."""
        project_name = self.config.get_project_name()
        backend_tls_config = self.config.get_backend_tls_config()
        max_request_body_bytes = validated_request_body_limit(
            self.config.get_manifest_processor_config().get(
                "max_request_body_bytes", DEFAULT_MAX_REQUEST_BODY_BYTES
            )
        )
        registry_region = self.config.get_global_region()
        root_ca_parameter_name = backend_tls_root_ca_parameter_name(project_name)
        registry_parameter_arn = (
            f"arn:{self.partition}:ssm:{registry_region}:{self.account}:"
            f"parameter/{project_name}/alb-hostname-{self.deployment_region}"
        )
        root_ca_parameter_arn = (
            f"arn:{self.partition}:ssm:{registry_region}:{self.account}:"
            f"parameter/{root_ca_parameter_name.lstrip('/')}"
        )

        role = iam.Role(
            self,
            "InferenceStreamingProxyRole",
            assumed_by=iam.ServicePrincipal("lambda.amazonaws.com"),
            managed_policies=[
                iam.ManagedPolicy.from_aws_managed_policy_name(
                    "service-role/AWSLambdaVPCAccessExecutionRole"
                )
            ],
        )
        role.add_to_policy(
            iam.PolicyStatement(
                effect=iam.Effect.ALLOW,
                actions=[
                    "secretsmanager:GetSecretValue",
                    "secretsmanager:DescribeSecret",
                ],
                resources=[f"{self.auth_secret_arn}*"],
            )
        )
        role.add_to_policy(
            iam.PolicyStatement(
                effect=iam.Effect.ALLOW,
                actions=["ssm:GetParameter"],
                resources=[registry_parameter_arn, root_ca_parameter_arn],
            )
        )
        role.add_to_policy(
            iam.PolicyStatement(
                effect=iam.Effect.ALLOW,
                actions=[
                    "elasticloadbalancing:DescribeLoadBalancers",
                    "elasticloadbalancing:DescribeTags",
                ],
                resources=["*"],
            )
        )

        from gco.stacks.nag_suppressions import acknowledge_nag_findings

        acknowledge_nag_findings(
            role,
            [
                {
                    "id": "AwsSolutions-IAM5",
                    "reason": (
                        "ELB ownership verification and the Lambda VPC/X-Ray APIs do not "
                        "support resource-level scoping. Secret and SSM reads remain "
                        "scoped to this deployment's exact resources."
                    ),
                    "appliesTo": ["Resource::*"],
                }
            ],
        )

        log_group = logs.LogGroup(
            self,
            "InferenceStreamingProxyLogGroup",
            retention=logs.RetentionDays.ONE_WEEK,
            removal_policy=RemovalPolicy.DESTROY,
        )
        return lambda_.Function(
            self,
            "InferenceStreamingProxyFunction",
            function_name=(f"{project_name}-regional-inference-proxy-{self.deployment_region}"),
            runtime=getattr(lambda_.Runtime, LAMBDA_NODEJS_RUNTIME),
            handler="index.handler",
            code=lambda_.Code.from_asset("lambda/inference-streaming-proxy-build"),
            timeout=Duration.minutes(15),
            memory_size=256,
            role=role,
            vpc=self.vpc,
            vpc_subnets=ec2.SubnetSelection(subnet_type=ec2.SubnetType.PRIVATE_WITH_EGRESS),
            security_groups=[self._proxy_lambda_security_group],
            environment={
                "ROUTING_MODE": "regional",
                "MAX_REQUEST_BODY_BYTES": str(max_request_body_bytes),
                "SECRET_ARN": self.auth_secret_arn,
                "REGISTRY_REGION": registry_region,
                "TARGET_REGION": self.deployment_region,
                "PROJECT_NAME": project_name,
                "AWS_ACCOUNT_ID": self.account,
                "AWS_URL_SUFFIX": self.url_suffix,
                "BACKEND_TLS_SERVER_NAME": backend_tls_server_name(project_name),
                "BACKEND_TLS_ROOT_CA_PARAMETER": root_ca_parameter_name,
                "BACKEND_TLS_ROOT_CA_REGION": registry_region,
                "BACKEND_TLS_CA_CACHE_TTL_SECONDS": str(
                    backend_tls_config["trust_cache_ttl_seconds"]
                ),
                "BACKEND_TLS_CA_MAX_STALE_SECONDS": str(
                    backend_tls_config["trust_cache_max_stale_seconds"]
                ),
            },
            log_group=log_group,
            description=(
                f"Regional inference response-streaming proxy for {self.deployment_region}"
            ),
            tracing=lambda_.Tracing.ACTIVE,
        )

    def _create_api_gateway(self) -> apigateway.RestApi:
        """Create regional API Gateway with IAM authentication."""
        project_name = self.config.get_project_name()

        # Create CloudWatch log group
        # log_group_name intentionally omitted - let CDK generate unique name
        api_log_group = logs.LogGroup(
            self,
            "ApiGatewayLogs",
            retention=logs.RetentionDays.ONE_MONTH,
            removal_policy=RemovalPolicy.DESTROY,
        )

        api_config = self.config.get_api_gateway_config()
        configured_log_level = str(api_config["log_level"]).upper()
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

        # Create regional REST API
        api = apigateway.RestApi(
            self,
            "RegionalApi",
            rest_api_name=f"{project_name}-regional-api-{self.deployment_region}",
            description=f"Direct regional API for {project_name} in {self.deployment_region}",
            endpoint_types=[apigateway.EndpointType.REGIONAL],
            deploy=True,
            deploy_options=apigateway.StageOptions(
                stage_name="prod",
                throttling_rate_limit=api_config["throttle_rate_limit"],
                throttling_burst_limit=api_config["throttle_burst_limit"],
                logging_level=logging_levels[configured_log_level],
                # Never put inference prompts/responses (or other API bodies)
                # into execution logs. Standard access logs and metrics remain.
                data_trace_enabled=False,
                metrics_enabled=api_config["metrics_enabled"],
                tracing_enabled=api_config["tracing_enabled"],
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
            # CDK otherwise retains the generated API Gateway account role.
            cloud_watch_role_removal_policy=RemovalPolicy.DESTROY,
        )

        # The bridge is private at the authorization layer by default: only
        # the aggregator execution role is named in the API resource policy.
        api.add_to_resource_policy(
            iam.PolicyStatement(
                effect=iam.Effect.ALLOW,
                principals=[iam.ArnPrincipal(self.aggregator_role_arn)],
                actions=["execute-api:Invoke"],
                resources=[
                    f"execute-api:/*/{method}/{path}"
                    for method, path in AGGREGATOR_REGIONAL_API_ROUTES
                ],
            )
        )

        # Direct regional mode is an explicit opt-in in the commercial
        # partition. It becomes the required supported ingress in partitions
        # where Global Accelerator does not exist. Methods still require SigV4
        # and callers still need identity-policy permission to invoke.
        if (
            self.config.get_api_gateway_config()["regional_api_enabled"]
            or not self.global_accelerator_enabled
        ):
            api.add_to_resource_policy(
                iam.PolicyStatement(
                    effect=iam.Effect.ALLOW,
                    principals=[iam.AnyPrincipal()],
                    actions=["execute-api:Invoke"],
                    resources=["execute-api:/*"],
                    conditions={
                        "StringEquals": {"aws:PrincipalAccount": self.account},
                        "ArnNotEquals": {"aws:PrincipalArn": self.aggregator_role_arn},
                    },
                )
            )

        # Keep control-plane integration semantics unchanged. Inference uses
        # InvokeWithResponseStream and may remain open for API Gateway's full
        # 15-minute streaming integration window; request bodies are buffered.
        control_plane_integration = apigateway.LambdaIntegration(
            self.proxy_lambda, proxy=True, timeout=Duration.seconds(29)
        )
        inference_integration = apigateway.LambdaIntegration(
            self.inference_proxy_lambda,
            proxy=True,
            timeout=Duration.minutes(15),
            response_transfer_mode=apigateway.ResponseTransferMode.STREAM,
        )

        # API Gateway greedy resources do not cross a root segment, so
        # /api/v1/{proxy+} cannot match /inference/{endpoint}/....
        api_resource = api.root.add_resource("api")
        v1_resource = api_resource.add_resource("v1")
        api_proxy_resource = v1_resource.add_resource("{proxy+}")
        inference_resource = api.root.add_resource("inference")
        inference_proxy_resource = inference_resource.add_resource("{proxy+}")

        for method in ["GET", "HEAD", "POST", "PUT", "DELETE", "PATCH", "OPTIONS"]:
            api_proxy_resource.add_method(
                method,
                control_plane_integration,
                authorization_type=apigateway.AuthorizationType.IAM,
                method_responses=[
                    apigateway.MethodResponse(status_code="200"),
                    apigateway.MethodResponse(status_code="400"),
                    apigateway.MethodResponse(status_code="403"),
                    apigateway.MethodResponse(status_code="500"),
                ],
            )

        for method in ["GET", "HEAD", "POST"]:
            inference_proxy_resource.add_method(
                method,
                inference_integration,
                authorization_type=apigateway.AuthorizationType.IAM,
                method_responses=[
                    apigateway.MethodResponse(status_code="200"),
                    apigateway.MethodResponse(status_code="400"),
                    apigateway.MethodResponse(status_code="404"),
                    apigateway.MethodResponse(status_code="500"),
                    apigateway.MethodResponse(status_code="502"),
                ],
            )

        from gco.stacks.nag_suppressions import acknowledge_nag_findings

        acknowledge_nag_findings(
            api.deployment_stage,
            [
                {
                    "id": "AwsSolutions-APIG3",
                    "reason": (
                        "This regional bridge is not a general public API: every method "
                        "requires SigV4 and its resource policy admits only the exact "
                        "aggregator role unless account-local direct access is explicitly "
                        "enabled. A separate WAF would duplicate those identity controls."
                    ),
                },
                {
                    "id": "NIST.800.53.R5-APIGWAssociatedWithWAF",
                    "reason": (
                        "The IAM-authenticated regional bridge has an aggregator-only resource "
                        "policy by default; unauthorized traffic is rejected before integration."
                    ),
                },
                {
                    "id": "PCI.DSS.321-APIGWAssociatedWithWAF",
                    "reason": (
                        "The IAM-authenticated regional bridge has an aggregator-only resource "
                        "policy by default and carries no payment-card-specific public surface."
                    ),
                },
            ],
        )

        return api

    def _create_outputs(self) -> None:
        """Export regional API Gateway endpoint."""
        project_name = self.config.get_project_name()

        CfnOutput(
            self,
            "RegionalApiEndpoint",
            value=self.api.url,
            description=f"Regional API Gateway endpoint for {self.deployment_region}",
            export_name=f"{project_name}-regional-api-endpoint-{self.deployment_region}",
        )
