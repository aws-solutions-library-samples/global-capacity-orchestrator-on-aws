"""End-to-end cdk-nag compliance regression test.

What this catches
-----------------
The class ``TestCdkNagCompliance`` synthesizes the full CDK application
exactly the way ``app.py`` does — Global, API Gateway, one or more
Regional stacks, and the Monitoring stack — with the five cdk-nag rule
packs registered as CDK policy-validation plugins. After ``app.synth()``
returns, the test reads the cloud assembly's ``validation-report.json``
and asserts that no unacknowledged findings survived.

Why read the validation report
-------------------------------
cdk-nag v3 runs as an ``IPolicyValidationPlugin`` (CDK's native policy
validation framework) rather than as an ``IAspect`` that emits synth
annotations. Violations are written to ``validation-report.json`` in the
cloud assembly directory; ``app.synth()`` itself does **not** raise on
findings (the CDK toolkit sets a non-zero exit code instead), so a plain
in-process synth would silently pass. Reading the report gives us a
deterministic, in-process signal: every unacknowledged finding lands in
the report and the test fails with the rule id, resource path, and a
short description — the three things you need to either scope an existing
acknowledgment or add a new one.

Scope
-----
Parameterized across the IAM-relevant subset of the cdk.json
configuration matrix (``tests/_cdk_config_matrix.NAG_CONFIGS``): the
configs that produce distinct IAM policy surfaces (default,
multi-region, FSx-enabled, all-features-enabled, three-regions, plus
the analytics-environment fixtures). The full CONFIGS matrix runs
via ``tests/test_cdk_synthesis_matrix.py`` for synthesis correctness;
this test focuses on the configs that actually change IAM roles and
policies, which is where cdk-nag findings live.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import aws_cdk as cdk
import pytest

from tests._cdk_config_matrix import NAG_CONFIGS as _CONFIGS


def _build_app(
    context_overrides: dict[str, Any] | None = None,
) -> cdk.App:
    """Construct a CDK ``App`` configured the same way ``app.py`` does, with
    the five cdk-nag rule packs registered as policy-validation plugins.

    Args:
        context_overrides: cdk.json context keys to override. Merged
            into the baseline cdk.json context before the app is
            built — this is how each parameterized config exercises a
            different code path (multi-region, FSx on, etc.).

    Returns:
        The constructed (but not yet synthesized) ``cdk.App``. Call
        ``app.synth()`` and then :func:`_collect_nag_violations` to read
        the findings.

    The Docker asset and helm-installer Lambda are mocked out the
    same way every other regional-stack test mocks them, so no
    Docker daemon is required during pytest.
    """
    project_root = Path(__file__).resolve().parent.parent

    # Load the baseline cdk.json context.
    cdk_json_path = project_root / "cdk.json"
    with cdk_json_path.open() as f:
        cdk_json = json.load(f)
    context: dict[str, Any] = dict(cdk_json.get("context", {}))

    # Apply overrides. Dict values are merged shallow-ly so partial
    # overrides (e.g. ``{"eks_cluster": {"endpoint_access": "PUBLIC_AND_PRIVATE"}}``)
    # don't clobber unrelated keys in the same block.
    if context_overrides:
        for key, value in context_overrides.items():
            if isinstance(value, dict) and key in context and isinstance(context[key], dict):
                merged = dict(context[key])
                merged.update(value)
                context[key] = merged
            else:
                context[key] = value

    app = cdk.App(context=context)

    # Register the nag packs exactly the way ``app.py`` does: the
    # LambdaTracingAspect (a real Aspect) runs during synthesis, then the
    # five rule packs run as policy-validation plugins against the
    # synthesized templates.
    from app import LambdaTracingAspect
    from gco.stacks.nag_suppressions import nag_validation_plugins

    cdk.Aspects.of(app).add(LambdaTracingAspect())
    cdk.Validations.of(app).add_plugins(*nag_validation_plugins(app, verbose=True))

    return app


def _collect_nag_violations(app: cdk.App) -> list[dict[str, Any]]:
    """Read every unacknowledged finding from the app's validation report.

    cdk-nag writes ``validation-report.json`` into the cloud assembly
    directory (``app.outdir``) during ``app.synth()``. Returns one dict per
    finding with ``pack`` / ``rule`` / ``description`` / ``paths`` so the
    assertion message can point at exactly what fired and where.
    """
    report_path = Path(app.outdir) / "validation-report.json"
    if not report_path.exists():
        return []
    data = json.loads(report_path.read_text())
    findings: list[dict[str, Any]] = []
    for plugin_report in data.get("pluginReports", []):
        pack = plugin_report.get("pluginName", "?")
        for violation in plugin_report.get("violations", []):
            findings.append(
                {
                    "pack": pack,
                    "rule": violation.get("ruleName", "?"),
                    "description": violation.get("description", ""),
                    "paths": [
                        c.get("constructPath", "?")
                        for c in violation.get("violatingConstructs", [])
                    ],
                }
            )
    return findings


def _format_violations(findings: list[dict[str, Any]]) -> str:
    """Multi-line, deterministic summary suitable for a pytest assertion message."""
    lines: list[str] = []
    for f in sorted(findings, key=lambda x: (x["pack"], x["rule"], "".join(x["paths"]))):
        for path in f["paths"] or ["(no construct path)"]:
            lines.append(f"  [{f['pack']}] {f['rule']} at {path}")
        info = f["description"]
        if len(info) > 200:
            info = info[:197] + "..."
        lines.append(f"    -> {info}")
    return "\n".join(lines) if lines else "(no findings)"


def _build_all_stacks(app: cdk.App, account: str | None = None) -> None:
    """Instantiate every stack ``app.py`` builds: global, API gateway,
    one regional stack per configured region, monitoring, and — when
    ``analytics_environment.enabled=true`` — the optional
    ``GCOAnalyticsStack``. Matches ``app.py::main`` one-for-one so the
    cdk-nag findings captured here are the same ones a
    ``cdk deploy --all`` would surface.

    Args:
        app: the CDK app to attach the stacks to.
        account: when provided, every stack is created with a concrete
            ``env`` account (mirroring ``app.py`` resolving
            ``CDK_DEFAULT_ACCOUNT`` at deploy time). This flips the
            account from the ``<AWS::AccountId>`` pseudo-parameter to a
            literal in synthesized ARNs, which is the exact rendering
            that a ``cdk deploy`` performs and that agnostic-only synth
            never exercises. Leave ``None`` for the environment-agnostic
            case.

    The heavy per-stack mocks (Docker asset + helm installer) are
    applied with ``patch.object`` inside the caller's ``with`` block;
    this function just wires the stacks together.
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
        env=cdk.Environment(account=account, region=global_region),
    )

    api_gateway_stack = GCOApiGatewayGlobalStack(
        app,
        f"{project_name}-api-gateway",
        global_accelerator_dns=global_stack.get_accelerator_dns_name(),
        project_name=project_name,
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
        global_stack.add_regional_endpoint(region, regional_stack.alb_arn)  # type: ignore[arg-type]

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

    # Mirror ``app.py``'s conditional analytics-stack instantiation so
    # the ``analytics-enabled`` / ``analytics-enabled-hyperpod`` fixtures
    # in ``NAG_CONFIGS`` actually exercise the SageMaker / Cognito / EMR
    # Serverless cdk-nag surface. When the toggle is off, this branch is
    # skipped and the matrix behaves exactly as before.
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


def _mock_helm_installer(stack: Any) -> None:
    """Mock ``_create_helm_installer_lambda`` so tests don't need a
    Docker daemon. Sets every attribute downstream consumers
    (monitoring_stack, regional_stack's own post-helm pipeline) read
    off of the helm-installer Lambda.
    """
    stack.helm_installer_lambda = MagicMock()
    stack.helm_installer_provider = MagicMock()
    # nosec B106 — test fixture ARN, not a real credential.
    stack.helm_installer_provider.service_token = (
        "arn:aws:lambda:us-east-1:123456789012:function:mock"
    )
    # monitoring_stack reads this as a plain string for widget setup;
    # it must be a concrete name, not a Token.
    stack.helm_installer_lambda_function_name = (
        f"gco-helm-{getattr(stack, 'deployment_region', 'us-east-1')}"
    )


class TestCdkNagCompliance:
    """End-to-end regression: ``app.synth()`` must produce zero
    unacknowledged cdk-nag findings across every representative
    configuration.

    When a test fails, the assertion message lists every finding by
    rule ID, resource path, and a short description — the same three
    pieces of information you'd need to either scope an existing
    acknowledgment or add a new one.
    """

    # The IAM-relevant config subset is shared with
    # ``tests/test_cdk_synthesis_matrix.py`` via
    # ``tests/_cdk_config_matrix.NAG_CONFIGS``. Only configs that
    # produce distinct IAM policy surfaces are included — the rest
    # (helm toggles, thresholds, etc.) don't change IAM and would
    # just burn CI time. See the module docstring in
    # ``_cdk_config_matrix.py`` for the rationale.
    CONFIGS: list[tuple[str, dict[str, Any]]] = _CONFIGS

    @pytest.mark.parametrize("config_name,overrides", CONFIGS, ids=[c[0] for c in CONFIGS])
    def test_no_unsuppressed_findings(self, config_name: str, overrides: dict[str, Any]) -> None:
        from cli.stacks import cdk_asset_consumer
        from gco.stacks.regional_stack import GCORegionalStack

        project_root = Path(__file__).resolve().parent.parent
        with cdk_asset_consumer(project_root):
            app = _build_app(context_overrides=overrides)

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

        findings = _collect_nag_violations(app)
        assert not findings, (
            f"cdk-nag found {len(findings)} unacknowledged finding(s) "
            f"with config {config_name!r}.\n\n{_format_violations(findings)}\n\n"
            f"Each finding either needs its underlying wildcard scoped "
            f"further or a targeted ``acknowledge_nag_findings`` entry with a "
            f"justification and an ``applies_to`` scoped to the "
            f"specific resource. Do NOT add broad ``Resource::*`` or "
            f"``Action::*`` entries — those defeat the whole point of "
            f"cdk-nag."
        )

    # An obviously-fake 12-digit account used only to force
    # environment-specific synthesis. It never reaches AWS — it just makes CDK
    # render account-bearing ARNs as a literal instead of the <AWS::AccountId>
    # pseudo-parameter.
    _CONCRETE_ACCOUNT = "123456789012"

    def test_no_unsuppressed_findings_with_concrete_account(self) -> None:
        """The full app must also synthesize with zero unacknowledged cdk-nag
        findings when the stacks are environment-specific (a concrete ``env``
        account).

        ``app.py`` resolves ``CDK_DEFAULT_ACCOUNT`` into every stack's ``env``
        so the regional VPC can enumerate the region's real AZ list. That flips
        account-bearing ARNs from the ``<AWS::AccountId>`` pseudo-parameter to a
        literal — exactly the rendering a ``cdk deploy`` uses, and one the
        agnostic ``test_no_unsuppressed_findings`` parametrization never
        exercises. This is the regression guard for suppressions whose
        ``applies_to`` previously matched only the pseudo-parameter form (see
        ``acknowledge_nag_findings`` in ``gco/stacks/nag_suppressions.py``). The
        account rendering is config-independent, so the default config suffices.
        """
        from cli.stacks import cdk_asset_consumer
        from gco.stacks.regional_stack import GCORegionalStack

        project_root = Path(__file__).resolve().parent.parent
        with cdk_asset_consumer(project_root):
            app = _build_app()

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

                _build_all_stacks(app, account=self._CONCRETE_ACCOUNT)
                app.synth()

        findings = _collect_nag_violations(app)
        assert not findings, (
            f"cdk-nag found {len(findings)} unacknowledged finding(s) when synthesizing with "
            f"a concrete env account ({self._CONCRETE_ACCOUNT}).\n\n"
            f"{_format_violations(findings)}\n\n"
            f"These do NOT appear in the environment-agnostic matrix because account-bearing "
            f"ARNs render as the <AWS::AccountId> pseudo-parameter there, but as a literal "
            f"account id under a real ``cdk deploy``. Fix by making the acknowledgment's "
            f"applies_to match both renderings — ``acknowledge_nag_findings`` registers both "
            f"forms when the stack account is concrete."
        )
