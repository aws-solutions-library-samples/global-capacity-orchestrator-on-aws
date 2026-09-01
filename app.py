#!/usr/bin/env python3
"""
GCO (Global Capacity Orchestrator on AWS) - Multi-Region EKS Auto Mode Platform for AI/ML Workloads

This is the main CDK application entry point that orchestrates the deployment of:
- Global Stack: partition-wide state plus AWS Global Accelerator in the commercial `aws` partition
- API Gateway Stack: Centralized IAM-authenticated entry point
- Regional Stacks: EKS clusters, internal ALBs, and services per region
- Regional API Bridges: SigV4 entry points with VPC Lambdas for aggregation and direct access (optional in `aws`, required elsewhere)
- Monitoring Stack: Cross-region CloudWatch dashboards and alarms
- Optional Analytics Stack: SageMaker Studio and EMR Serverless

Architecture:
    Commercial `aws`: User → API Gateway (IAM Auth) → Global Accelerator → Internal Regional ALB → EKS Services
    Other partitions: User → Regional API Gateway (IAM Auth) → VPC Lambda → Internal Regional ALB → EKS Services
    Aggregator → Regional API Gateway (SigV4) → VPC Lambda → Internal Regional ALB

Usage:
    cdk deploy --all                    # Deploy all stacks
    cdk deploy gco-us-east-1            # Deploy single region
    cdk destroy --all                   # Cleanup all resources
"""

import os
from pathlib import Path

import aws_cdk as cdk
import jsii
from constructs import IConstruct

from cli.stacks import cdk_asset_consumer
from gco.config.config_loader import ConfigLoader
from gco.stacks.analytics_stack import GCOAnalyticsStack
from gco.stacks.api_gateway_global_stack import AnalyticsApiConfig, GCOApiGatewayGlobalStack
from gco.stacks.global_stack import GCOGlobalStack
from gco.stacks.monitoring_stack import GCOMonitoringStack
from gco.stacks.nag_suppressions import nag_validation_plugins
from gco.stacks.regional_api_gateway_stack import GCORegionalApiGatewayStack
from gco.stacks.regional_stack import GCORegionalStack

# <pyflowchart-code-diagram> BEGIN - auto-inserted, do not edit
# Generated at (UTC): 2026-09-01T14:42:56Z
# Generated from Git commit: 89b000378ed5a912a38c06f4feab2b029936ebcc
# Flowchart(s) generated from this file:
#   * ``main`` -> ``diagrams/code_diagrams/app.main.html``
#     (PNG: ``diagrams/code_diagrams/app.main.png``)
# Regenerate with ``SOURCE_DATE_EPOCH=<unix-seconds> GCO_DIAGRAM_SOURCE_COMMIT=<40-char-sha> python diagrams/generate.py --code-only``.
# <pyflowchart-code-diagram> END


# AWS Solutions guidance identifier. Only the GCO *global* stack description is
# prefixed with this string, so a single deployment is attributable to the
# published guidance (SO9707) through one stack.
SOLUTION_ID = "SO9707"
SOLUTION_DESCRIPTION_PREFIX = (
    f"({SOLUTION_ID}) - Guidance for EKS AutoMode Clusters with Global Capacity Orchestrator on AWS"
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


@cdk_asset_consumer(Path(__file__).resolve().parent)
def main() -> None:
    """
    Main application entry point.

    Creates and configures all CDK stacks with proper dependencies:
    1. Global stack (shared state plus optional Global Accelerator) - must be created first
    2. API Gateway stack - uses Global Accelerator DNS only when available
    3. Regional stacks - depend on both global stacks
    4. Regional API bridges - depend on their matching regional stack
       (direct caller access is optional in `aws` and required elsewhere)
    5. Monitoring stack - depends on all regional stacks
    6. Optional analytics stack - feeds Studio routes into the API Gateway stack
    """
    app = cdk.App()

    # Enable cdk-nag compliance rule packs. These validate the synthesized
    # CloudFormation templates against security best practices. Any violations
    # that aren't explicitly acknowledged (see nag_suppressions.py) are written
    # to the cloud assembly's policy-validation report.
    # Note: These are rule packs, not certifications — passing cdk-nag does not
    # make the deployment automatically compliant with these frameworks.

    # The X-Ray tracing aspect must run before the nag packs *see* the
    # templates. In cdk-nag v3 the packs are IPolicyValidationPlugins that
    # validate the synthesized templates AFTER every Aspect has run, so
    # registering the aspect here guarantees tracing=Active is already set by
    # the time the Serverless pack checks for it.
    cdk.Aspects.of(app).add(LambdaTracingAspect())

    # Register the five rule packs (AWS Solutions, HIPAA, NIST 800-53 R5,
    # PCI DSS 3.2.1, Serverless) as CDK policy-validation plugins. Each pack
    # reads the acknowledgment metadata written by ``acknowledge_nag_findings``
    # natively, so the packs run directly.
    cdk.Validations.of(app).add_plugins(*nag_validation_plugins(app, verbose=True))

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
    api_gateway_config = config.get_api_gateway_config()
    manifest_processor_config = config.get_manifest_processor_config()

    # Apply common tags to all stacks
    for key, value in tags.items():
        cdk.Tags.of(app).add(key, value)

    # Resolve the target AWS account for every stack. Deploying (or even
    # synthesizing against real infrastructure) requires valid credentials, so
    # the CDK CLI populates CDK_DEFAULT_ACCOUNT from the active identity. Pairing
    # it with each stack's region makes the stacks *environment-specific*, which
    # is what lets CDK's availability-zones context provider look up the real AZ
    # list for the account+region. The regional VPC relies on that lookup to
    # place a subnet in every AZ (regional_stack.py uses max_azs=99). When the
    # variable is unset — e.g. an environment-agnostic ``cdk synth`` in CI — this
    # is None and stacks stay agnostic exactly as before.
    account = os.environ.get("CDK_DEFAULT_ACCOUNT")

    # Create global resources. Global Accelerator itself is included only in
    # the commercial ``aws`` partition; the shared data plane remains
    # available everywhere through regional IAM-authenticated API bridges.
    global_stack = GCOGlobalStack(
        app,
        f"{project_name}-global",
        config=config,
        env=cdk.Environment(account=account, region=global_region),
        description=f"{SOLUTION_DESCRIPTION_PREFIX} - Shared global resources for GCO (Global Capacity Orchestrator on AWS)",
    )

    # Create global API Gateway stack (authenticated entry point)
    api_gateway_stack = GCOApiGatewayGlobalStack(
        app,
        f"{project_name}-api-gateway",
        global_accelerator_dns=global_stack.get_accelerator_dns_name(),
        project_name=project_name,
        api_gateway_config=api_gateway_config,
        registry_region=global_region,
        certificate_regions=regional_regions,
        backend_tls_config=config.get_backend_tls_config(),
        max_request_body_bytes=manifest_processor_config.get("max_request_body_bytes", 1_048_576),
        env=cdk.Environment(account=account, region=api_gateway_region),
        description="Global API Gateway with IAM authentication",
    )
    api_gateway_stack.add_stack_dependency(global_stack)

    # Create regional stacks for each configured region
    regional_stacks = []
    for region in regional_regions:
        regional_stack = GCORegionalStack(
            app,
            f"{project_name}-{region}",
            config=config,
            region=region,
            auth_secret_arn=api_gateway_stack.secret.secret_arn,
            env=cdk.Environment(account=account, region=region),
            description=f"Regional resources for {region} - EKS cluster, ALB, and services",
        )

        # Add dependencies
        regional_stack.add_stack_dependency(global_stack)
        regional_stack.add_stack_dependency(api_gateway_stack)
        regional_stacks.append(regional_stack)

        # Every region gets an IAM-authenticated API bridge so the centralized
        # aggregator can reach the private ALB through a VPC-attached Lambda.
        # In commercial ``aws``, ``regional_api_enabled`` controls whether
        # other account principals may invoke the bridge directly. Other
        # partitions enable that IAM-authenticated workload ingress
        # automatically. Neither mode disables the aggregator's required path.
        regional_api_stack = GCORegionalApiGatewayStack(
            app,
            f"{project_name}-regional-api-{region}",
            config=config,
            region=region,
            vpc=regional_stack.vpc,
            auth_secret_arn=api_gateway_stack.secret.secret_arn,
            aggregator_role_arn=api_gateway_stack.aggregator_role.role_arn,
            env=cdk.Environment(account=account, region=region),
            description=f"Regional aggregation and workload bridge for {region}",
        )
        regional_api_stack.add_stack_dependency(regional_stack)

    # Create monitoring stack
    monitoring_stack = GCOMonitoringStack(
        app,
        f"{project_name}-monitoring",
        config=config,
        global_stack=global_stack,
        regional_stacks=regional_stacks,
        api_gateway_stack=api_gateway_stack,
        env=cdk.Environment(account=account, region=monitoring_region),
        description="Cross-region monitoring and observability for GCO (Global Capacity Orchestrator on AWS)",
    )

    # Add dependencies on all regional stacks
    for regional_stack in regional_stacks:
        monitoring_stack.add_stack_dependency(regional_stack)

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
            env=cdk.Environment(account=account, region=api_gateway_region),
            description="Optional ML and analytics environment (SageMaker Studio, EMR Serverless, Cognito)",
        )
        analytics_stack.add_stack_dependency(global_stack)

        # Wire the analytics stack's presigned-URL Lambda into the API
        # Gateway stack via a mutator. The API gateway stack was already
        # created above (before the analytics stack) because every
        # regional stack declares a dependency on it; re-ordering the
        # two globals would ripple through the entire stack graph. The
        # mutator lets us defer the /studio/* wiring until both stacks
        # exist without changing the existing dependency chain.
        #
        # ``api_gateway_stack.add_stack_dependency(analytics_stack)`` ensures
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
                f"execute-api.{api_gateway_region}."
                f"{api_gateway_stack.url_suffix}/prod/studio/callback"
            ),
        )
        api_gateway_stack.set_analytics_config(analytics_api_config)
        api_gateway_stack.add_stack_dependency(analytics_stack)

    app.synth()


if __name__ == "__main__":
    main()
