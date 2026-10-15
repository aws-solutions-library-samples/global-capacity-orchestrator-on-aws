"""The full-app CDK builder behind every test that synthesizes the whole app.

``app.py::main`` is what ``cdk deploy --all`` synthesizes. These suites build the
same stacks through :func:`build_all_stacks` instead of keeping their own copies:

* the cdk-nag gate (``tests/test_nag_compliance.py``) and
  ``scripts/dump_nag_findings.py``,
* the synthesis matrix (``tests/test_cdk_synthesis_matrix.py``),
* the project-name scoping and analytics property suites, through
  ``tests/_analytics_cdk_overlays.py``.

There used to be three copies. They drifted apart from app.py and from each
other: one never built the regional API bridge stacks or passed the API Gateway
stack app.py's arguments, so neither the gate nor the scoping tests ever saw that
IAM and those names (PR #461). ``tests/test_cdk_harness_contract.py`` now fails if
this builder stops matching ``app.py::main`` (stack classes, constructor keywords,
the calls made on each stack) or if another module grows its own full-app builder.

The one stand-in is :func:`offline_image_assets`. Everything else synthesizes for
real, including the Helm installer Lambda and its convergence state machine,
which an earlier helper replaced with a mock and so left out of every template
these suites checked.
"""

from __future__ import annotations

import contextlib
import json
from collections.abc import Iterator, Mapping
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import aws_cdk as cdk
from aws_cdk import cx_api

PROJECT_ROOT = Path(__file__).resolve().parent.parent

#: Image URI every stood-in ``DockerImageAsset`` reports.
STAND_IN_IMAGE_URI = "123456789012.dkr.ecr.us-east-1.amazonaws.com/test:latest"


def load_context(overrides: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """Return ``cdk.json``'s context with ``overrides`` merged over it.

    A dict override is merged one level deep into the block of the same name, so
    ``{"eks_cluster": {"endpoint_access": "PRIVATE"}}`` keeps the block's other
    keys. Any other value replaces the key outright.
    """
    cdk_json = json.loads((PROJECT_ROOT / "cdk.json").read_text(encoding="utf-8"))
    context: dict[str, Any] = dict(cdk_json.get("context", {}))
    for key, value in (overrides or {}).items():
        if isinstance(value, dict) and isinstance(context.get(key), dict):
            context[key] = {**context[key], **value}
        else:
            context[key] = value
    return context


@contextlib.contextmanager
def offline_image_assets() -> Iterator[MagicMock]:
    """Stand in for the regional stack's service images so synthesis needs no Docker.

    Each platform service image is a ``DockerImageAsset`` whose build context is
    the repository root, which CDK would fingerprint and stage on every synth.
    The stand-in reports :data:`STAND_IN_IMAGE_URI` instead. The Helm installer's
    image is staged from ``lambda/helm-installer-build`` without Docker, so it is
    left real.
    """
    with patch("gco.stacks.regional_stack.ecr_assets.DockerImageAsset") as image_asset:
        image_asset.return_value.image_uri = STAND_IN_IMAGE_URI
        yield image_asset


def build_all_stacks(app: cdk.App, account: str | None = None) -> None:
    """Add every stack ``app.py::main`` builds to ``app``, with the same arguments.

    Global, API Gateway, one regional stack and one regional API bridge per
    configured Region, monitoring, and the analytics stack when
    ``analytics_environment.enabled`` is true, wired with app.py's dependencies.
    Only ``description`` may differ from app.py: it is template prose, and all
    but the analytics stack leave it out.

    Args:
        app: the CDK app to add the stacks to. Its context is the configuration.
        account: a concrete ``env`` account for every stack, the way app.py uses
            ``CDK_DEFAULT_ACCOUNT`` under ``cdk deploy``. Account-bearing ARNs then
            render as a literal instead of the ``<AWS::AccountId>`` pseudo
            parameter. ``None`` keeps the stacks environment-agnostic.
    """
    from gco.config.config_loader import ConfigLoader
    from gco.stacks.analytics_stack import GCOAnalyticsStack
    from gco.stacks.api_gateway_global_stack import (
        AnalyticsApiConfig,
        GCOApiGatewayGlobalStack,
    )
    from gco.stacks.global_stack import GCOGlobalStack
    from gco.stacks.monitoring_stack import GCOMonitoringStack
    from gco.stacks.regional_api_gateway_stack import GCORegionalApiGatewayStack
    from gco.stacks.regional_stack import GCORegionalStack

    config = ConfigLoader(app)
    project_name = config.get_project_name()
    deployment_regions = config.get_deployment_regions()

    global_region = deployment_regions["global"]
    api_gateway_region = deployment_regions["api_gateway"]
    monitoring_region = deployment_regions["monitoring"]
    regional_regions = deployment_regions["regional"]
    manifest_processor_config = config.get_manifest_processor_config()

    global_stack = GCOGlobalStack(
        app,
        f"{project_name}-global",
        config=config,
        env=cdk.Environment(account=account, region=global_region),
    )

    api_gateway_stack = GCOApiGatewayGlobalStack(
        app,
        f"{project_name}-api-gateway",
        global_accelerator_dns=global_stack.get_accelerator_dns_name(),
        project_name=project_name,
        api_gateway_config=config.get_api_gateway_config(),
        registry_region=global_region,
        certificate_regions=regional_regions,
        backend_tls_config=config.get_backend_tls_config(),
        max_request_body_bytes=manifest_processor_config.get("max_request_body_bytes", 1_048_576),
        env=cdk.Environment(account=account, region=api_gateway_region),
    )
    api_gateway_stack.add_stack_dependency(global_stack)

    regional_stacks = []
    for region in regional_regions:
        regional_stack = GCORegionalStack(
            app,
            f"{project_name}-{region}",
            config=config,
            region=region,
            auth_secret_arn=api_gateway_stack.secret.secret_arn,
            env=cdk.Environment(account=account, region=region),
        )
        regional_stack.add_stack_dependency(global_stack)
        regional_stack.add_stack_dependency(api_gateway_stack)
        regional_stacks.append(regional_stack)

        regional_api_stack = GCORegionalApiGatewayStack(
            app,
            f"{project_name}-regional-api-{region}",
            config=config,
            region=region,
            vpc=regional_stack.vpc,
            auth_secret_arn=api_gateway_stack.secret.secret_arn,
            aggregator_role_arn=api_gateway_stack.aggregator_role.role_arn,
            env=cdk.Environment(account=account, region=region),
        )
        regional_api_stack.add_stack_dependency(regional_stack)

    monitoring_stack = GCOMonitoringStack(
        app,
        f"{project_name}-monitoring",
        config=config,
        global_stack=global_stack,
        regional_stacks=regional_stacks,
        api_gateway_stack=api_gateway_stack,
        env=cdk.Environment(account=account, region=monitoring_region),
    )
    for regional_stack in regional_stacks:
        monitoring_stack.add_stack_dependency(regional_stack)

    if config.get_analytics_enabled():
        analytics_stack = GCOAnalyticsStack(
            app,
            f"{project_name}-analytics",
            config=config,
            env=cdk.Environment(account=account, region=api_gateway_region),
            description=(
                "Optional ML and analytics environment (SageMaker Studio, EMR Serverless, Cognito)"
            ),
        )
        analytics_stack.add_stack_dependency(global_stack)

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


def synthesize_app(app: cdk.App, *, account: str | None = None) -> cx_api.CloudAssembly:
    """Build every app.py stack into ``app`` and synthesize it offline.

    The generated Lambda build directories are held stable for the whole synth
    (``cli.stacks.cdk_asset_consumer``) and the service images are stood in
    (:func:`offline_image_assets`). ``app`` supplies the context and, for the
    cdk-nag gate, the rule packs; ``account`` goes to :func:`build_all_stacks`.
    """
    from cli.stacks import cdk_asset_consumer

    with cdk_asset_consumer(PROJECT_ROOT), offline_image_assets():
        build_all_stacks(app, account=account)
        return app.synth()
