"""Shared overlay and synthesis helpers for the analytics-environment
property tests.

Two public entry points:

* :func:`build_overlay` — shape a ``cdk.json`` context dict from the
  ``(enabled, hyperpod_enabled, regions)`` tuple the Hypothesis strategies
  in :mod:`tests._analytics_strategies` produce.

* :func:`synth_all_stacks` — build the full CDK app the same way
  ``tests/test_nag_compliance.py::_build_all_stacks`` does, synthesize it,
  and return every stack's CloudFormation template as a plain ``dict``.

Both helpers mirror ``app.py`` one-for-one so the templates returned here
are the same ones a ``cdk deploy --all`` would see. The Docker asset and
helm-installer Lambda are patched out so no Docker daemon is required in
the hot property-test loop.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import aws_cdk as cdk

# ---------------------------------------------------------------------------
# build_overlay — shape a ``cdk.json`` context dict from a toggle tuple.
# ---------------------------------------------------------------------------


def build_overlay(
    enabled: bool,
    hyperpod_enabled: bool,
    regions: list[str],
    *,
    canvas_enabled: bool = False,
) -> dict[str, Any]:
    """Return a cdk.json context overlay dict for the given toggle state.

    The returned dict is suitable for passing as ``context_overrides`` to
    :func:`synth_all_stacks`. Only the keys this feature cares about are
    set — the baseline cdk.json context supplies everything else.

    Args:
        enabled: Value for ``analytics_environment.enabled``.
        hyperpod_enabled: Value for ``analytics_environment.hyperpod.enabled``.
        regions: Regional regions for ``deployment_regions.regional``. The
            global / api-gateway / monitoring regions default to
            ``us-east-2`` to match the baseline cdk.json. If the caller
            needs different non-regional regions they can layer another
            overlay on top of the returned dict.
        canvas_enabled: Value for ``analytics_environment.canvas.enabled``.
            Defaults to ``False`` because most existing callers only
            care about the ``enabled`` × ``hyperpod_enabled`` matrix.

    Returns:
        A shallow-merge-compatible dict shaped like the relevant slice of
        ``cdk.json.context``.
    """
    return {
        "analytics_environment": {
            "enabled": enabled,
            "hyperpod": {"enabled": hyperpod_enabled},
            "canvas": {"enabled": canvas_enabled},
            "cognito": {"domain_prefix": None, "removal_policy": "destroy"},
            "efs": {"removal_policy": "destroy"},
            "studio": {"user_profile_name_prefix": None},
        },
        "deployment_regions": {
            "global": "us-east-2",
            "api_gateway": "us-east-2",
            "monitoring": "us-east-2",
            "regional": list(regions),
        },
    }


# ---------------------------------------------------------------------------
# synth_all_stacks — full-app synth returning {stack_name: template_dict}.
# ---------------------------------------------------------------------------


def _merge_context(baseline: dict[str, Any], overrides: dict[str, Any]) -> dict[str, Any]:
    """Shallow-merge dict-valued keys, replace scalar/list keys.

    Mirrors the merge semantics used by
    ``tests/test_nag_compliance.py::_build_app``.
    """
    context = dict(baseline)
    for key, value in overrides.items():
        if isinstance(value, dict) and key in context and isinstance(context[key], dict):
            merged = dict(context[key])
            merged.update(value)
            context[key] = merged
        else:
            context[key] = value
    return context


def _load_baseline_context() -> dict[str, Any]:
    """Read ``cdk.json``'s baseline context. Kept as a module-level helper
    so callers can sanity-check against the same file ``app.py`` reads.
    """
    cdk_json_path = Path(__file__).resolve().parent.parent / "cdk.json"
    with cdk_json_path.open() as f:
        cdk_json = json.load(f)
    return dict(cdk_json.get("context", {}))


def _mock_helm_installer(stack: Any) -> None:
    """Stand-in for ``GCORegionalStack._create_helm_installer_lambda``.

    Matches the shape expected by the downstream monitoring stack +
    regional post-helm pipeline (helm_installer_lambda, provider,
    service_token, lambda function name). Taken from
    ``tests/test_nag_compliance.py::_mock_helm_installer``.
    """
    stack.helm_installer_lambda = MagicMock()
    stack.helm_installer_provider = MagicMock()
    # nosec B106 — test fixture ARN, not a real credential.
    stack.helm_installer_provider.service_token = (
        "arn:aws:lambda:us-east-1:123456789012:function:mock"
    )
    stack.helm_installer_lambda_function_name = (
        f"gco-helm-{getattr(stack, 'deployment_region', 'us-east-1')}"
    )


def _build_all_stacks(app: cdk.App) -> None:
    """Wire every stack ``app.py`` builds into ``app``.

    Mirrors ``app.py::main`` and ``tests/test_nag_compliance.py::
    _build_all_stacks`` exactly so the templates produced here match a
    real ``cdk deploy --all``. The analytics stack is instantiated iff
    ``config.get_analytics_enabled()`` is true, matching the conditional
    in ``app.py``.
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
        global_accelerator_dns=global_stack.accelerator.dns_name,
        env=cdk.Environment(region=api_gateway_region),
    )
    api_gateway_stack.add_dependency(global_stack)

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
        regional_stack.add_dependency(global_stack)
        regional_stack.add_dependency(api_gateway_stack)
        regional_stacks.append(regional_stack)
        # alb_arn is set during regional stack construction.
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
        monitoring_stack.add_dependency(regional_stack)

    if config.get_analytics_enabled():
        analytics_stack = GCOAnalyticsStack(
            app,
            f"{project_name}-analytics",
            config=config,
            env=cdk.Environment(region=api_gateway_region),
            description=(
                "Optional ML and analytics environment (SageMaker Studio, EMR Serverless, Cognito)"
            ),
        )
        analytics_stack.add_dependency(global_stack)

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


def synth_all_stacks(overlay: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Synthesize the full CDK app with the given context overlay.

    Loads the baseline cdk.json context, shallow-merges ``overlay`` over
    it, builds every stack ``app.py`` builds (including the optional
    ``GCOAnalyticsStack`` when the analytics toggle is on), synthesizes,
    and returns a ``{stack_name: CloudFormation_template_dict}`` dict.

    The Docker asset and helm-installer Lambda are patched out so no
    Docker daemon is required.

    Args:
        overlay: cdk.json context overlay, typically produced by
            :func:`build_overlay`. Shallow-merged over the baseline.

    Returns:
        ``{stack_name: template_dict}`` — every stack the app produced,
        keyed by its construct id (e.g. ``gco-global``,
        ``gco-api-gateway``, ``gco-us-east-1``, ``gco-monitoring``,
        ``gco-analytics``).
    """
    from gco.stacks.regional_stack import GCORegionalStack

    baseline = _load_baseline_context()
    context = _merge_context(baseline, overlay)

    app = cdk.App(context=context)

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
        assembly = app.synth()

    templates: dict[str, dict[str, Any]] = {}
    for artifact in assembly.stacks:
        templates[artifact.stack_name] = dict(artifact.template)
    return templates
