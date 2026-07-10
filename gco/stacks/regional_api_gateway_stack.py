"""
Regional API Gateway stack for private EKS cluster access.

This stack creates a regional API Gateway with a VPC Lambda that can access
internal ALBs directly. This enables API access when public access is disabled
(internal ALB only).

Use Case:
    When eks_cluster.endpoint_access is PRIVATE and you want the ALB to also
    be internal-only, this stack provides authenticated API access via a
    Lambda function deployed inside the VPC.

Architecture:
    API Gateway (Regional) → VPC Lambda → Internal ALB → EKS pods

Security:
    - API Gateway uses IAM authentication (SigV4)
    - Lambda runs inside the VPC with access to internal ALB
    - Same auth token validation as the global path
    - No public exposure of ALB or EKS API

Configuration:
    Enable in cdk.json:
    {
        "api_gateway": {
            "regional_api_enabled": true
        }
    }
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
from gco.stacks.constants import LAMBDA_PYTHON_RUNTIME

# <pyflowchart-code-diagram> BEGIN - auto-inserted, do not edit
# Flowchart(s) generated from this file:
#   * ``GCORegionalApiGatewayStack.__init__`` -> ``diagrams/code_diagrams/gco/stacks/regional_api_gateway_stack.GCORegionalApiGatewayStack___init__.html``
#     (PNG: ``diagrams/code_diagrams/gco/stacks/regional_api_gateway_stack.GCORegionalApiGatewayStack___init__.png``)
# Regenerate with ``python diagrams/code_diagrams/generate.py``.
# <pyflowchart-code-diagram> END


class GCORegionalApiGatewayStack(Stack):
    """
    Regional API Gateway with VPC Lambda for private cluster access.

    This stack enables API access when the ALB is internal-only by deploying
    a Lambda function inside the VPC that can reach the internal ALB directly.

    Attributes:
        api: Regional REST API with IAM authentication
        proxy_lambda: VPC Lambda that forwards requests to internal ALB
    """

    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        config: ConfigLoader,
        region: str,
        vpc: ec2.IVpc,
        alb_dns_name: str,
        auth_secret_arn: str,
        **kwargs: Any,
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)

        self.config = config
        self.deployment_region = region
        self.vpc = vpc
        self.alb_dns_name = alb_dns_name
        self.auth_secret_arn = auth_secret_arn

        # Create VPC Lambda for proxying requests
        self.proxy_lambda = self._create_vpc_proxy_lambda()

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
            project_name=self.config.get_project_name(),
        )

    def _create_vpc_proxy_lambda(self) -> lambda_.Function:
        """Create VPC Lambda that proxies requests to internal ALB."""
        project_name = self.config.get_project_name()

        # Create security group for Lambda
        lambda_sg = ec2.SecurityGroup(
            self,
            "ProxyLambdaSg",
            vpc=self.vpc,
            description="Security group for regional API proxy Lambda",
            allow_all_outbound=True,
        )

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

        # Grant read access to auth secret
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

        # Create log group
        # log_group_name intentionally omitted - let CDK generate unique name
        log_group = logs.LogGroup(
            self,
            "ProxyLambdaLogGroup",
            retention=logs.RetentionDays.ONE_WEEK,
            removal_policy=RemovalPolicy.DESTROY,
        )

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
            environment={
                "ALB_ENDPOINT": self.alb_dns_name,
                "SECRET_ARN": self.auth_secret_arn,
            },
            log_group=log_group,
            description=f"Regional API proxy for {self.deployment_region} (VPC Lambda)",
            tracing=lambda_.Tracing.ACTIVE,
        )

        return proxy_lambda

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

        # Create regional REST API
        api = apigateway.RestApi(
            self,
            "RegionalApi",
            rest_api_name=f"{project_name}-regional-api-{self.deployment_region}",
            description=f"Regional API for {project_name} in {self.deployment_region} (private access)",
            endpoint_types=[apigateway.EndpointType.REGIONAL],
            deploy=True,
            deploy_options=apigateway.StageOptions(
                stage_name="prod",
                throttling_rate_limit=1000,
                throttling_burst_limit=2000,
                logging_level=apigateway.MethodLoggingLevel.INFO,
                data_trace_enabled=True,
                metrics_enabled=True,
                tracing_enabled=True,
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

        # Create Lambda integration
        lambda_integration = apigateway.LambdaIntegration(
            self.proxy_lambda, proxy=True, timeout=Duration.seconds(29)
        )

        # Create /api/v1 resource structure
        api_resource = api.root.add_resource("api")
        v1_resource = api_resource.add_resource("v1")

        # Add proxy resource to catch all paths
        proxy_resource = v1_resource.add_resource("{proxy+}")

        # Add methods with IAM authentication
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
