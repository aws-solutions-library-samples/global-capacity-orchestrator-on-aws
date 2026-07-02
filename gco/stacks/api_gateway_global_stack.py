"""
Global API Gateway stack - Single authenticated entry point for all regions.

This stack creates the centralized API Gateway that serves as the authenticated
entry point for all GCO API requests. It provides:
- Edge-optimized endpoint with CloudFront for global edge caching and DDoS protection
- IAM authentication (AWS SigV4) for all requests
- Lambda proxy that adds secret header for backend validation
- Secrets Manager secret with automatic rotation for request authentication
- Multi-region replication for the auth secret
- CloudWatch logging for audit and debugging

Security Flow:
    1. Client signs request with AWS credentials (SigV4)
    2. CloudFront edge location receives request (managed by AWS)
    3. API Gateway validates IAM permissions
    4. Lambda proxy retrieves secret from Secrets Manager
    5. Lambda adds X-GCO-Auth-Token header
    6. Request forwarded to Global Accelerator
    7. Backend services validate the secret header

Secret Rotation:
    The auth token is automatically rotated daily. During rotation:
    - A new token is generated and stored as AWSPENDING
    - Backend services accept both AWSCURRENT and AWSPENDING tokens
    - After validation, AWSPENDING becomes AWSCURRENT
    - Multi-region replication ensures all regions receive the new token

This ensures all traffic goes through the authenticated path and prevents
direct access to the Global Accelerator or regional ALBs.
"""

import json
from dataclasses import dataclass
from typing import Any

from aws_cdk import (
    CfnOutput,
    Duration,
    Fn,
    RemovalPolicy,
    Stack,
)
from aws_cdk import aws_apigateway as apigateway
from aws_cdk import aws_cognito as cognito
from aws_cdk import aws_iam as iam
from aws_cdk import aws_lambda as lambda_
from aws_cdk import aws_logs as logs
from aws_cdk import aws_secretsmanager as secretsmanager
from aws_cdk import aws_wafv2 as wafv2
from constructs import Construct

from gco.stacks.constants import LAMBDA_PYTHON_RUNTIME

# <pyflowchart-code-diagram> BEGIN - auto-inserted, do not edit
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
        secret: Secrets Manager secret for backend validation
        proxy_lambda: Lambda function that proxies requests to Global Accelerator
        aggregator_lambda: Lambda function for cross-region aggregation
        api: REST API with IAM authentication
    """

    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        global_accelerator_dns: str,
        regional_endpoints: dict[str, str] | None = None,
        analytics_config: AnalyticsApiConfig | None = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)

        self.ga_dns = global_accelerator_dns
        self.regional_endpoints = regional_endpoints or {}
        # When analytics is disabled (the default) this stays ``None`` and
        # the stack synthesizes exactly as it did pre-analytics. When
        # non-``None``, ``_wire_studio_routes`` is invoked at the end of
        # the constructor, after the IAM-authorized ``/api/v1/*`` and
        # ``/inference/*`` methods are already attached — so Cognito and
        # IAM authorization coexist at the method level rather than at
        # the API level.
        self.analytics_config: AnalyticsApiConfig | None = analytics_config

        # Create secret token for ALB validation
        self.secret = self._create_secret()

        # Create proxy Lambda
        self.proxy_lambda = self._create_proxy_lambda()

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

        # API Gateway stack needs global_region for SSM parameter access suppressions
        # The aggregator Lambda reads ALB hostnames from SSM in the global region
        apply_all_suppressions(self, stack_type="api_gateway", global_region=self.region)

    def _create_secret(self) -> secretsmanager.Secret:
        """Create secret token for validating requests from API Gateway.

        The secret is configured with:
        - Automatic rotation every 30 days
        - A rotation Lambda that generates new secure random tokens
        - Multi-region replication can be enabled via add_replica_region()
        """
        secret = secretsmanager.Secret(
            self,
            "GCOAuthSecret",
            secret_name="gco/api-gateway-auth-token",  # nosec B106 — this is the secret path, not a password
            description="Secret token for validating requests from API Gateway to ALB (auto-rotated)",
            generate_secret_string=secretsmanager.SecretStringGenerator(
                secret_string_template=json.dumps({"description": "GCO API Gateway auth token"}),
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
            description="Rotates the GCO API Gateway auth token",
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
        """Create Lambda function that proxies requests to Global Accelerator."""

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
            },
            log_group=proxy_lambda_log_group,
            tracing=lambda_.Tracing.ACTIVE,
        )

        # cdk-nag suppression: the proxy Lambda's execution role needs
        # broad network access for VPC Lambda execution.
        from gco.stacks.nag_suppressions import acknowledge_nag_findings

        acknowledge_nag_findings(
            lambda_role,
            [
                {
                    "id": "AwsSolutions-IAM5",
                    "reason": (
                        "The API Gateway proxy Lambda forwards requests to regional ALBs. "
                        "Its execution role needs broad network access "
                        "(ec2:CreateNetworkInterface, etc.) for VPC Lambda execution. "
                        "These APIs do not support resource-level scoping."
                    ),
                    "appliesTo": ["Resource::*"],
                },
            ],
        )

        return proxy_lambda

    def _create_aggregator_lambda(self) -> lambda_.Function:
        """Create Lambda function for cross-region aggregation.

        This Lambda queries all regional ALBs in parallel and aggregates
        the results for global views of jobs, health, and metrics.
        """
        # Create IAM role
        aggregator_role = iam.Role(
            self,
            "AggregatorLambdaRole",
            assumed_by=iam.ServicePrincipal("lambda.amazonaws.com"),
            managed_policies=[
                iam.ManagedPolicy.from_aws_managed_policy_name(
                    "service-role/AWSLambdaBasicExecutionRole"
                )
            ],
        )

        # Grant read access to secret
        self.secret.grant_read(aggregator_role)

        # Grant SSM read access for discovering regional endpoints
        aggregator_role.add_to_policy(
            iam.PolicyStatement(
                effect=iam.Effect.ALLOW,
                actions=["ssm:GetParametersByPath", "ssm:GetParameter"],
                resources=[f"arn:aws:ssm:{self.region}:{self.account}:parameter/gco/*"],
            )
        )

        # Create log group for Lambda
        aggregator_log_group = logs.LogGroup(
            self,
            "AggregatorLambdaLogGroup",
            retention=logs.RetentionDays.ONE_WEEK,
            removal_policy=RemovalPolicy.DESTROY,
        )

        # Create Lambda function
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
                "SECRET_ARN": self.secret.secret_arn,
                "PROJECT_NAME": "gco",
                "GLOBAL_REGION": self.region,
            },
            log_group=aggregator_log_group,
            description="Aggregates data from all regional GCO clusters",
            tracing=lambda_.Tracing.ACTIVE,
        )

        # cdk-nag suppression: the aggregator Lambda reads SSM parameters
        # and invokes regional endpoints.
        from gco.stacks.nag_suppressions import acknowledge_nag_findings

        acknowledge_nag_findings(
            aggregator_role,
            [
                {
                    "id": "AwsSolutions-IAM5",
                    "reason": (
                        "The cross-region aggregator Lambda reads SSM parameters and "
                        "invokes regional endpoints. Its execution role needs "
                        "ssm:GetParameter on the project's parameter namespace and "
                        "secretsmanager:GetSecretValue for the auth token."
                    ),
                    "appliesTo": ["Resource::*"],
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
            log_group_name="/aws/apigateway/gco-global",
            retention=logs.RetentionDays.ONE_MONTH,
            removal_policy=RemovalPolicy.DESTROY,
        )

        # Create REST API with edge-optimized endpoint
        # Edge-optimized uses CloudFront for global edge caching and DDoS protection
        api = apigateway.RestApi(
            self,
            "GCOGlobalApi",
            rest_api_name="gco-global-api",
            description="Global authenticated API for GCO (Global Capacity Orchestrator on AWS) (edge-optimized)",
            endpoint_types=[apigateway.EndpointType.EDGE],
            deploy=True,
            deploy_options=apigateway.StageOptions(
                stage_name="prod",
                throttling_rate_limit=1000,
                throttling_burst_limit=2000,
                logging_level=apigateway.MethodLoggingLevel.INFO,
                data_trace_enabled=True,
                metrics_enabled=True,
                tracing_enabled=True,  # Enable X-Ray tracing for request analysis
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

        # Create Lambda integration
        lambda_integration = apigateway.LambdaIntegration(
            self.proxy_lambda, proxy=True, timeout=Duration.seconds(29)
        )

        # Create /api resource
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

        # Create global aggregation routes
        self._create_global_routes(api, v1_resource)

        # Create inference proxy route
        # /inference/{proxy+} → proxy Lambda → GA → ALB → K8s Ingress
        self._create_inference_routes(api, lambda_integration)

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
            ANY /inference/{proxy+} → proxy Lambda → GA → ALB → K8s Ingress

        This allows authenticated inference requests to flow through the
        API Gateway with IAM auth, then get proxied to the regional ALB
        where K8s Ingress routes them to the correct inference Service.
        """
        inference_resource = api.root.add_resource("inference")
        inference_proxy = inference_resource.add_resource("{proxy+}")

        for method in ["GET", "POST", "PUT", "DELETE", "PATCH"]:
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
            export_name="gco-global-api-endpoint",
        )

        CfnOutput(
            self,
            "SecretArn",
            value=self.secret.secret_arn,
            description="Secret ARN for ALB validation",
            export_name="gco-auth-secret-arn",
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
            authorizer_name="gco-studio-cognito-authorizer",
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
            request_validator_name="gco-studio-request-validator",
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
            export_name="gco-studio-cognito-authorizer-id",
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
            export_name="gco-studio-login-url",
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
            log_group_name="aws-waf-logs-gco-api-gateway",
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
        #   1-6 -> AWS Managed Rule Groups
        waf_config = self.node.try_get_context("waf") or {}
        per_ip_rate_limit = int(waf_config.get("per_ip_rate_limit", 100))

        self.web_acl = wafv2.CfnWebACL(
            self,
            "GCOWebAcl",
            name="gco-api-gateway-waf",
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
                # Rule 1: AWS Managed Rules - Common Rule Set (OWASP Top 10)
                wafv2.CfnWebACL.RuleProperty(
                    name="AWSManagedRulesCommonRuleSet",
                    priority=1,
                    override_action=wafv2.CfnWebACL.OverrideActionProperty(none={}),
                    statement=wafv2.CfnWebACL.StatementProperty(
                        managed_rule_group_statement=wafv2.CfnWebACL.ManagedRuleGroupStatementProperty(
                            vendor_name="AWS",
                            name="AWSManagedRulesCommonRuleSet",
                        )
                    ),
                    visibility_config=wafv2.CfnWebACL.VisibilityConfigProperty(
                        cloud_watch_metrics_enabled=True,
                        metric_name="AWSManagedRulesCommonRuleSet",
                        sampled_requests_enabled=True,
                    ),
                ),
                # Rule 2: AWS Managed Rules - Known Bad Inputs
                wafv2.CfnWebACL.RuleProperty(
                    name="AWSManagedRulesKnownBadInputsRuleSet",
                    priority=2,
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
                # Rule 3: AWS Managed Rules - SQL Injection
                wafv2.CfnWebACL.RuleProperty(
                    name="AWSManagedRulesSQLiRuleSet",
                    priority=3,
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
                # Rule 4: AWS Managed Rules - Linux OS (protects against Linux-specific attacks)
                wafv2.CfnWebACL.RuleProperty(
                    name="AWSManagedRulesLinuxRuleSet",
                    priority=4,
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
                # Rule 5: AWS Managed Rules - Amazon IP Reputation List
                wafv2.CfnWebACL.RuleProperty(
                    name="AWSManagedRulesAmazonIpReputationList",
                    priority=5,
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
                # Rule 6: AWS Managed Rules - Anonymous IP List (blocks Tor, VPNs, proxies)
                wafv2.CfnWebACL.RuleProperty(
                    name="AWSManagedRulesAnonymousIpList",
                    priority=6,
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
            export_name="gco-waf-webacl-arn",
        )
