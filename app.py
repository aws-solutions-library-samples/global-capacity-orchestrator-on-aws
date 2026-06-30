#!/usr/bin/env python3
"""
GCO (Global Capacity Orchestrator on AWS) - Multi-Region EKS Auto Mode Platform for AI/ML Workloads

This is the main CDK application entry point that orchestrates the deployment of:
- Global Stack: AWS Global Accelerator for multi-region routing
- API Gateway Stack: Centralized IAM-authenticated entry point
- Regional Stacks: EKS clusters, ALBs, and services per region
- Monitoring Stack: Cross-region CloudWatch dashboards and alarms

Architecture:
    User → API Gateway (IAM Auth) → Global Accelerator → Regional ALB → EKS Services

Usage:
    cdk deploy --all                    # Deploy all stacks
    cdk deploy gco-us-east-1            # Deploy single region
    cdk destroy --all                   # Cleanup all resources
"""

import aws_cdk as cdk
import jsii
from cdk_nag import (
    AwsSolutionsChecks,
    HIPAASecurityChecks,
    NIST80053R5Checks,
    PCIDSS321Checks,
    ServerlessChecks,
)
from constructs import IConstruct

from gco.config.config_loader import ConfigLoader
from gco.stacks.analytics_stack import GCOAnalyticsStack
from gco.stacks.api_gateway_global_stack import AnalyticsApiConfig, GCOApiGatewayGlobalStack
from gco.stacks.global_stack import GCOGlobalStack
from gco.stacks.monitoring_stack import GCOMonitoringStack
from gco.stacks.regional_stack import GCORegionalStack

# <pyflowchart-code-diagram> BEGIN - auto-inserted, do not edit
# Flowchart(s) generated from this file:
#   * ``main`` -> ``diagrams/code_diagrams/app.main.html``
#     (PNG: ``diagrams/code_diagrams/app.main.png``)
# Regenerate with ``python diagrams/code_diagrams/generate.py``.
# <pyflowchart-code-diagram> END


# AWS Solutions guidance identifier. Only the GCO *global* stack description is
# prefixed with this string, so a single deployment is attributable to the
# published guidance (SO9707) through one stack. The guidance crawler counts
# every stack whose description carries the (SO9707) marker, so prefixing all
# stacks would over-count one deployment as many. Every other stack keeps just
# its own specific description (no prefix).
SOLUTION_ID = "SO9707"
SOLUTION_DESCRIPTION_PREFIX = (
    f"({SOLUTION_ID}) - Guidance for Automated Deployment of EKS AutoMode Clusters "
    "with Global Capacity Orchestrator on AWS"
)


@jsii.implements(cdk.IAspect)
class LambdaTracingAspect:
    """CDK Aspect that enables X-Ray tracing on all Lambda functions.

    This catches CDK Provider Framework Lambdas that we don't create directly,
    ensuring every Lambda in the stack has tracing=ACTIVE.
    """

    def visit(self, node: IConstruct) -> None:
        if isinstance(node, cdk.aws_lambda.CfnFunction):
            node.tracing_config = cdk.aws_lambda.CfnFunction.TracingConfigProperty(mode="Active")


def main() -> None:
    """
    Main application entry point.

    Creates and configures all CDK stacks with proper dependencies:
    1. Global stack (Global Accelerator) - must be created first
    2. API Gateway stack - depends on Global Accelerator DNS
    3. Regional stacks - depend on both global stacks
    4. Monitoring stack - depends on all regional stacks
    """
    app = cdk.App()

    # Enable cdk-nag compliance rule packs. These validate all CDK constructs
    # against security best practices during synthesis. Any violations that
    # aren't explicitly suppressed (see nag_suppressions.py) will fail the build.
    # Note: These are rule packs, not certifications — passing cdk-nag does not
    # make the deployment automatically compliant with these frameworks.

    # IMPORTANT: Register the tracing aspect BEFORE nag checks. CDK Aspects run
    # in registration order — if nag checks run first, they see Lambda functions
    # without tracing and emit Serverless-LambdaTracing warnings.
    cdk.Aspects.of(app).add(LambdaTracingAspect())

    cdk.Aspects.of(app).add(AwsSolutionsChecks(verbose=True))  # AWS architecture best practices
    cdk.Aspects.of(app).add(HIPAASecurityChecks(verbose=True))  # Healthcare security rules
    cdk.Aspects.of(app).add(NIST80053R5Checks(verbose=True))  # Federal security controls
    cdk.Aspects.of(app).add(PCIDSS321Checks(verbose=True))  # Payment card industry rules
    cdk.Aspects.of(app).add(ServerlessChecks(verbose=True))  # Serverless best practices

    # Load configuration from cdk.json
    config = ConfigLoader(app)

    # Get configuration values
    project_name = config.get_project_name()
    deployment_regions = config.get_deployment_regions()
    tags = config.get_tags()

    # Extract region configurations
    global_region = deployment_regions["global"]
    api_gateway_region = deployment_regions["api_gateway"]
    monitoring_region = deployment_regions["monitoring"]
    regional_regions = deployment_regions["regional"]

    # Apply common tags to all stacks
    for key, value in tags.items():
        cdk.Tags.of(app).add(key, value)

    # Create global stack (Global Accelerator)
    # Note: Global Accelerator is a global service but needs a "home" region for CloudFormation.
    # This is the ONLY stack whose description carries the (SO9707) guidance prefix, so the
    # deployment stays attributable to the published guidance without over-counting (see SOLUTION_ID).
    global_stack = GCOGlobalStack(
        app,
        f"{project_name}-global",
        config=config,
        env=cdk.Environment(region=global_region),
        description=f"{SOLUTION_DESCRIPTION_PREFIX} - Global resources including AWS Global Accelerator for GCO (Global Capacity Orchestrator on AWS)",
    )

    # Create global API Gateway stack (authenticated entry point)
    api_gateway_stack = GCOApiGatewayGlobalStack(
        app,
        f"{project_name}-api-gateway",
        global_accelerator_dns=global_stack.accelerator.dns_name,
        env=cdk.Environment(region=api_gateway_region),
        description="Global API Gateway with IAM authentication",
    )
    api_gateway_stack.add_dependency(global_stack)

    # Create regional stacks for each configured region
    regional_stacks = []
    for region in regional_regions:
        regional_stack = GCORegionalStack(
            app,
            f"{project_name}-{region}",
            config=config,
            region=region,
            auth_secret_arn=api_gateway_stack.secret.secret_arn,
            env=cdk.Environment(region=region),
            description=f"Regional resources for {region} - EKS cluster, ALB, and services",
        )

        # Add dependencies
        regional_stack.add_dependency(global_stack)
        regional_stack.add_dependency(api_gateway_stack)
        regional_stacks.append(regional_stack)

        # Register regional ALB with Global Accelerator
        # alb_arn is set during regional stack construction; it's always populated
        # by the time CloudFormation processes the dependency chain.
        global_stack.add_regional_endpoint(region, regional_stack.alb_arn)  # type: ignore[arg-type]

    # Create monitoring stack
    monitoring_stack = GCOMonitoringStack(
        app,
        f"{project_name}-monitoring",
        config=config,
        global_stack=global_stack,
        regional_stacks=regional_stacks,
        api_gateway_stack=api_gateway_stack,
        env=cdk.Environment(region=monitoring_region),
        description="Cross-region monitoring and observability for GCO (Global Capacity Orchestrator on AWS)",
    )

    # Add dependencies on all regional stacks
    for regional_stack in regional_stacks:
        monitoring_stack.add_dependency(regional_stack)

    # Optionally instantiate the analytics stack when explicitly enabled via
    # cdk.json. The stack lives in the API gateway region so the
    # presigned-URL Lambda can be wired into the existing /studio/* API
    # Gateway routes without a cross-region hop.
    # When the toggle is off, the stack is skipped entirely so cdk synth
    # emits no SageMaker, EMR Serverless, or Cognito resources.
    if config.get_analytics_enabled():
        # Note: we intentionally do NOT pass ``api_gateway_secret_arn``
        # here. That kwarg is reserved for future auth wiring and is not
        # consumed by any CloudFormation resource. Passing the secret
        # ARN (a cross-stack token) would force an implicit
        # ``analytics_stack → api_gateway_stack`` dependency, which
        # would deadlock against the reverse dependency we add below
        # (api_gateway_stack needs the presigned-URL Lambda ARN).
        analytics_stack = GCOAnalyticsStack(
            app,
            f"{project_name}-analytics",
            config=config,
            env=cdk.Environment(region=api_gateway_region),
            description="Optional ML and analytics environment (SageMaker Studio, EMR Serverless, Cognito)",
        )
        analytics_stack.add_dependency(global_stack)

        # Wire the analytics stack's presigned-URL Lambda into the API
        # Gateway stack via a mutator. The API gateway stack was already
        # created above (before the analytics stack) because every
        # regional stack declares a dependency on it; re-ordering the
        # two globals would ripple through the entire stack graph. The
        # mutator lets us defer the /studio/* wiring until both stacks
        # exist without changing the existing dependency chain.
        #
        # ``api_gateway_stack.add_dependency(analytics_stack)`` ensures
        # the analytics stack (and its Lambda) finish deploying before
        # CloudFormation updates the API gateway stack — the Lambda
        # ARN is now a cross-stack reference on the API gateway side.
        analytics_api_config = AnalyticsApiConfig(
            user_pool_arn=analytics_stack.cognito_pool.user_pool_arn,
            user_pool_client_id=analytics_stack.cognito_client.user_pool_client_id,
            presigned_url_lambda=analytics_stack.presigned_url_lambda,
            studio_domain_name=analytics_stack.studio_domain.domain_name or "",
            callback_url=(
                f"https://{api_gateway_stack.api.rest_api_id}."
                f"execute-api.{api_gateway_region}.amazonaws.com/prod/studio/callback"
            ),
        )
        api_gateway_stack.set_analytics_config(analytics_api_config)
        api_gateway_stack.add_dependency(analytics_stack)

    app.synth()


if __name__ == "__main__":
    main()
