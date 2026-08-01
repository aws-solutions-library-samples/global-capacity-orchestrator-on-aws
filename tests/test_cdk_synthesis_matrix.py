"""CDK configuration-matrix synthesis test.

Parameterized over every entry in ``tests/_cdk_config_matrix.CONFIGS``
(the full set a user can pick from cdk.json — multi-region, FSx
toggles, analytics fixtures, and so on).

What this replaces
------------------
The previous workflow invoked the standalone script, which:
    * rewrote ``cdk.json`` in-place for each config (race-prone; can't
      parallelize without a lock),
    * shelled out to ``cdk synth --quiet`` 24 times, paying the
      Node.js + JSII cold start per invocation.

Running the same configs as an in-process pytest gives us:
    * serial execution in CI, avoiding CDK's shared in-process asset-staging
      cache races while still removing subprocess cold-start overhead,
    * context passed via ``cdk.App(context=…)`` — the supported
      CDK-level injection point, no file mutation,
    * the same Docker-asset and helm-installer Lambda mocks that
      ``test_nag_compliance.py`` already relies on, so Docker isn't
      required on the runner.

What this does *not* replace
----------------------------
``test_nag_compliance.py`` parameterizes over ``NAG_CONFIGS`` — a
curated IAM-relevant subset — and asserts zero unsuppressed cdk-nag
findings. That test stays as-is. This file asserts only that
``app.synth()`` completes without raising for every config in
``CONFIGS``, mirroring what the old script checked.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import aws_cdk as cdk
import pytest

from tests._cdk_config_matrix import CONFIGS


def _build_app(context_overrides: dict[str, Any]) -> cdk.App:
    """Construct a CDK ``App`` with the baseline cdk.json context plus
    the per-config overlay. Mirrors the merge rules in
    ``test_nag_compliance._build_app`` so a config here
    and a config there exercise identical code paths.
    """
    import json

    project_root = Path(__file__).resolve().parent.parent
    cdk_json_path = project_root / "cdk.json"
    with cdk_json_path.open() as f:
        cdk_json = json.load(f)
    context: dict[str, Any] = dict(cdk_json.get("context", {}))

    for key, value in context_overrides.items():
        if isinstance(value, dict) and key in context and isinstance(context[key], dict):
            merged = dict(context[key])
            merged.update(value)
            context[key] = merged
        else:
            context[key] = value

    return cdk.App(context=context)


def _mock_helm_installer(stack: Any) -> None:
    """Mock ``_create_helm_installer_lambda`` so synth doesn't need a
    Docker daemon. Same implementation as ``test_nag_compliance``.
    """
    stack.helm_installer_lambda = MagicMock()
    stack.helm_installer_provider = MagicMock()
    stack.helm_installer_provider.service_token = (
        "arn:aws:lambda:us-east-1:123456789012:function:mock"
    )
    stack.helm_installer_lambda_function_name = (
        f"gco-helm-{getattr(stack, 'deployment_region', 'us-east-1')}"
    )


def _build_all_stacks(app: cdk.App) -> None:
    """Instantiate every stack ``app.py::main`` builds. Kept in sync
    with ``test_nag_compliance._build_all_stacks``; if you add or
    remove a stack there, change it here too.
    """
    from gco.config.config_loader import ConfigLoader
    from gco.stacks.analytics_stack import GCOAnalyticsStack
    from gco.stacks.api_gateway_global_stack import (
        AnalyticsApiConfig,
        GCOApiGatewayGlobalStack,
    )
    from gco.stacks.global_stack import GCOGlobalStack
    from gco.stacks.monitoring_stack import GCOMonitoringStack
    from gco.stacks.regional_stack import GCORegionalStack

    config = ConfigLoader(app)
    project_name = config.get_project_name()
    deployment_regions = config.get_deployment_regions()

    global_region = deployment_regions["global"]
    api_gateway_region = deployment_regions["api_gateway"]
    monitoring_region = deployment_regions["monitoring"]
    regional_regions = deployment_regions["regional"]

    global_stack = GCOGlobalStack(
        app,
        f"{project_name}-global",
        config=config,
        env=cdk.Environment(region=global_region),
    )

    api_gateway_stack = GCOApiGatewayGlobalStack(
        app,
        f"{project_name}-api-gateway",
        global_accelerator_dns=global_stack.get_accelerator_dns_name(),
        env=cdk.Environment(region=api_gateway_region),
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
            env=cdk.Environment(region=region),
        )
        regional_stack.add_stack_dependency(global_stack)
        regional_stack.add_stack_dependency(api_gateway_stack)
        regional_stacks.append(regional_stack)
        global_stack.add_regional_endpoint(region, regional_stack.alb_arn)  # type: ignore[arg-type]

    monitoring_stack = GCOMonitoringStack(
        app,
        f"{project_name}-monitoring",
        config=config,
        global_stack=global_stack,
        regional_stacks=regional_stacks,
        api_gateway_stack=api_gateway_stack,
        env=cdk.Environment(region=monitoring_region),
    )
    for regional_stack in regional_stacks:
        monitoring_stack.add_stack_dependency(regional_stack)

    if config.get_analytics_enabled():
        analytics_stack = GCOAnalyticsStack(
            app,
            f"{project_name}-analytics",
            config=config,
            env=cdk.Environment(region=api_gateway_region),
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


@pytest.mark.parametrize("config_name,overrides", CONFIGS, ids=[c[0] for c in CONFIGS])
def test_synth_succeeds(config_name: str, overrides: dict[str, Any]) -> None:
    """Every config in the shared matrix must synthesize without raising."""
    from cli.stacks import cdk_asset_consumer
    from gco.stacks.regional_stack import GCORegionalStack

    project_root = Path(__file__).resolve().parent.parent
    with cdk_asset_consumer(project_root):
        app = _build_app(overrides)

        with (
            patch("gco.stacks.regional_stack.ecr_assets.DockerImageAsset") as mock_docker,
            patch.object(
                GCORegionalStack,
                "_create_helm_installer_lambda",
                _mock_helm_installer,
            ),
        ):
            mock_image = MagicMock()
            mock_image.image_uri = "123456789012.dkr.ecr.us-east-1.amazonaws.com/test:latest"
            mock_docker.return_value = mock_image

            _build_all_stacks(app)
            app.synth()
