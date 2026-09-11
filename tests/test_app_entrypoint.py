"""Covers ``app.py``, the CDK application entry point.

``app.py::main`` is what ``cdk synth`` / ``cdk deploy`` execute: it builds the
global, API Gateway, per-region regional and bridge, monitoring and (optional)
analytics stacks, wires their deployment order, applies the ``tags`` context,
resolves ``CDK_DEFAULT_ACCOUNT`` into every stack's environment, registers the
X-Ray tracing aspect plus the cdk-nag rule packs, and synthesizes. The tests
here run that function for real — a full in-process synth into ``tmp_path`` —
and pin the stack graph it produces. Whether the synthesized templates pass the
rule packs is the business of ``tests/test_nag_compliance.py``; this suite only
pins the wiring.

``main()`` constructs a bare ``cdk.App()`` with no arguments, which would write
the cloud assembly into a temporary directory of CDK's choosing and read no
context, so ``app.py``'s ``cdk`` module reference is swapped for a proxy whose
``App`` injects an ``outdir`` under ``tmp_path`` and the context the test wants.
Everything else on the proxy is the real ``aws_cdk``. Two seams keep the run
offline — the Docker image asset (no container daemon under pytest, as in every
other regional-stack suite) and the regional stack's credentialed EC2
Availability-Zone lookup — and ``boto3.client`` is replaced with a raiser so
nothing else can reach AWS. See :func:`_run_main`.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from unittest.mock import patch

import aws_cdk as cdk
import pytest
from aws_cdk import assertions, aws_lambda

import app as app_module

ROOT = Path(__file__).resolve().parents[1]

# Context override that enables the optional analytics environment (the same
# shape the cdk-nag and synthesis matrices use).
_ANALYTICS_CONTEXT: dict[str, Any] = {
    "analytics_environment": {"enabled": True, "hyperpod": {"enabled": False}},
}


class _CdkProxy:
    """``aws_cdk`` as seen by ``app.py``, with ``App`` bound to a known outdir/context."""

    def __init__(self, *, outdir: Path, context: dict[str, Any] | None) -> None:
        self._outdir = outdir
        self._context = context
        self.apps: list[cdk.App] = []

    def __getattr__(self, name: str) -> Any:
        return getattr(cdk, name)

    def App(self) -> cdk.App:
        """Stand in for the ``cdk.App`` class ``main()`` instantiates (hence the name)."""
        app = cdk.App(outdir=str(self._outdir), context=self._context)
        self.apps.append(app)
        return app


def _cdk_json_context(overrides: dict[str, Any]) -> dict[str, Any]:
    """The repository's ``cdk.json`` context with ``overrides`` merged in.

    ``cdk synth`` hands ``app.py`` everything under ``cdk.json``'s ``context``
    — the deployment configuration *and* the CDK feature flags (for example
    ``@aws-cdk/aws-ec2:restrictDefaultSecurityGroup``, without which the VPC
    stacks fail the rule packs). Mapping values are merged one level deep so a
    partial override keeps its siblings, mirroring ``tests/test_nag_compliance``.
    """
    context: dict[str, Any] = dict(json.loads((ROOT / "cdk.json").read_text())["context"])
    for key, value in overrides.items():
        if isinstance(value, dict) and isinstance(context.get(key), dict):
            context[key] = {**context[key], **value}
        else:
            context[key] = value
    return context


def _run_main(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    overrides: dict[str, Any],
    account: str | None = None,
) -> cdk.App:
    """Run ``app.main()`` with its assembly written under ``tmp_path``; return the app.

    Two seams keep this offline. ``DockerImageAsset`` is stubbed (no container
    daemon), and ``GCORegionalStack._resolve_unsupported_az_names`` — the one
    AWS call a *credentialed* synth makes, an EC2 AZ-ID-to-name lookup that the
    regional stack performs only when ``CDK_DEFAULT_ACCOUNT`` is set — returns
    an empty list. Without that, setting the variable to exercise
    account-specific rendering would reach EC2 and fail wherever credentials
    are absent (which is CI). The exclusion logic behind that lookup is covered
    by ``tests/test_regional_stack.py``; ``boto3.client`` is replaced with a
    raiser here so any other attempt to reach AWS during synthesis fails loudly
    rather than depending on the developer's credentials.
    """
    from gco.stacks.regional_stack import GCORegionalStack

    monkeypatch.chdir(ROOT)
    if account is None:
        monkeypatch.delenv("CDK_DEFAULT_ACCOUNT", raising=False)
    else:
        monkeypatch.setenv("CDK_DEFAULT_ACCOUNT", account)
    proxy = _CdkProxy(outdir=tmp_path / "cdk.out", context=_cdk_json_context(overrides))
    monkeypatch.setattr(app_module, "cdk", proxy)

    def _no_aws(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError(f"synthesis must not construct an AWS client: {args} {kwargs}")

    with (
        patch("gco.stacks.regional_stack.ecr_assets.DockerImageAsset") as mock_docker,
        patch.object(GCORegionalStack, "_resolve_unsupported_az_names", return_value=[]),
        patch("boto3.client", _no_aws),
    ):
        mock_docker.return_value.image_uri = (
            "123456789012.dkr.ecr.us-east-1.amazonaws.com/test:latest"
        )
        app_module.main()
    (app,) = proxy.apps
    return app


def _stacks(app: cdk.App) -> dict[str, cdk.Stack]:
    return {child.node.id: child for child in app.node.children if isinstance(child, cdk.Stack)}


def _depends_on(stack: cdk.Stack) -> set[str]:
    return {dependency.node.id for dependency in stack.dependencies}


def _regions(app: cdk.App) -> dict[str, Any]:
    return app_module.ConfigLoader(app).get_deployment_regions()


class TestLambdaTracingAspect:
    def test_every_lambda_function_gets_active_tracing(self) -> None:
        """Provider-framework functions the app never creates directly are traced too."""
        stack = cdk.Stack(cdk.App(), "s")
        function = aws_lambda.CfnFunction(
            stack, "Fn", code={"zip_file": "pass"}, role="arn:aws:iam::123456789012:role/r"
        )
        assert function.tracing_config is None

        app_module.LambdaTracingAspect().visit(function)

        assert function.tracing_config.mode == "Active"
        assertions.Template.from_stack(stack).has_resource_properties(
            "AWS::Lambda::Function", {"TracingConfig": {"Mode": "Active"}}
        )

    def test_other_constructs_are_left_alone(self) -> None:
        """The aspect visits every node; only ``AWS::Lambda::Function`` is touched."""
        stack = cdk.Stack(cdk.App(), "s")
        cdk.CfnResource(stack, "Bucket", type="AWS::S3::Bucket")

        app_module.LambdaTracingAspect().visit(stack)
        app_module.LambdaTracingAspect().visit(stack.node.find_child("Bucket"))

        assert assertions.Template.from_stack(stack).to_json()["Resources"] == {
            "Bucket": {"Type": "AWS::S3::Bucket"}
        }


class TestMain:
    def test_default_config_builds_the_stack_graph_without_analytics(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Global → API Gateway → regional + bridge per region → monitoring; no analytics.

        With ``CDK_DEFAULT_ACCOUNT`` unset the stacks stay environment-agnostic,
        the ``tags`` context is applied to every stack, only the global stack
        carries the solution identifier, and the tracing aspect plus the five
        rule packs are registered at the app scope.
        """
        app = _run_main(tmp_path, monkeypatch, overrides={})
        regions = _regions(app)
        stacks = _stacks(app)

        expected = {
            "gco-global",
            "gco-api-gateway",
            "gco-monitoring",
            *(f"gco-{region}" for region in regions["regional"]),
            *(f"gco-regional-api-{region}" for region in regions["regional"]),
        }
        assert set(stacks) == expected
        assembly = app.synth()  # already synthesized by main(); returns the cached assembly
        assert {artifact.stack_name for artifact in assembly.stacks} == expected
        assert Path(assembly.directory) == tmp_path / "cdk.out"

        assert stacks["gco-global"].region == regions["global"]
        assert stacks["gco-api-gateway"].region == regions["api_gateway"]
        assert stacks["gco-monitoring"].region == regions["monitoring"]
        for region in regions["regional"]:
            assert stacks[f"gco-{region}"].region == region
            assert stacks[f"gco-regional-api-{region}"].region == region
        assert all(cdk.Token.is_unresolved(stack.account) for stack in stacks.values())

        assert _depends_on(stacks["gco-api-gateway"]) == {"gco-global"}
        for region in regions["regional"]:
            assert _depends_on(stacks[f"gco-{region}"]) == {"gco-global", "gco-api-gateway"}
            assert _depends_on(stacks[f"gco-regional-api-{region}"]) == {f"gco-{region}"}
        assert _depends_on(stacks["gco-monitoring"]) >= {
            f"gco-{region}" for region in regions["regional"]
        }

        tags = _cdk_json_context({})["tags"]
        assert tags  # cdk.json ships common tags; every stack must carry them
        for stack in stacks.values():
            assert stack.tags.tag_values() == tags
        assert stacks["gco-global"].template_options.description.startswith(
            f"({app_module.SOLUTION_ID}) - Guidance for EKS AutoMode Clusters"
        )
        assert not any(
            (stack.template_options.description or "").startswith(f"({app_module.SOLUTION_ID})")
            for name, stack in stacks.items()
            if name != "gco-global"
        )

        assert len(app.policy_validation_beta1) == 5
        assert any(
            isinstance(applied.aspect, app_module.LambdaTracingAspect)
            for applied in cdk.Aspects.of(app).applied
        )

    def test_analytics_enabled_wires_studio_into_the_api_gateway(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The optional analytics stack is built and the API Gateway stack depends on it.

        ``CDK_DEFAULT_ACCOUNT`` makes every stack environment-specific, which is
        the rendering ``cdk deploy`` uses.
        """
        app = _run_main(tmp_path, monkeypatch, overrides=_ANALYTICS_CONTEXT, account="123456789012")
        regions = _regions(app)
        stacks = _stacks(app)

        assert "gco-analytics" in stacks
        assert stacks["gco-analytics"].region == regions["api_gateway"]
        assert _depends_on(stacks["gco-analytics"]) == {"gco-global"}
        assert _depends_on(stacks["gco-api-gateway"]) == {"gco-global", "gco-analytics"}
        assert all(stack.account == "123456789012" for stack in stacks.values())

        assembly = app.synth()
        assert "gco-analytics" in {artifact.stack_name for artifact in assembly.stacks}
        api_template = assembly.get_stack_by_name("gco-api-gateway").template
        assert any(
            resource["Type"] == "AWS::ApiGateway::Resource"
            and resource["Properties"].get("PathPart") == "studio"
            for resource in api_template["Resources"].values()
        )
