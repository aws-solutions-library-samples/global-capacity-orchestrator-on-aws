#!/usr/bin/env python3
"""Generate infrastructure diagrams for GCO with cdk-dia.

This script synthesizes the CDK app the same way ``app.py`` does and renders
one architecture diagram per stack type (global, api-gateway, regional,
regional-api, monitoring, analytics) plus a combined full-architecture
diagram, as PNG, using `cdk-dia <https://github.com/pistazie/cdk-dia>`_.

Why cdk-dia (and not aws-pdk)?
-----------------------------
The previous generator used ``aws-pdk``'s ``cdk-graph`` +
``cdk-graph-plugin-diagram``. Every ``aws-pdk`` release (through the latest,
0.26.15) hard-pins ``cdk-nag<3.0.0``, which is incompatible with the
cdk-nag 3.x this project now uses — it blocks the ``[cdk]`` extra and the
lockfile, and aws-pdk is end-of-life (its successor, the Nx Plugin for AWS,
is a TypeScript monorepo scaffolder that does not carry the diagram plugin
forward). ``cdk-dia`` is an actively maintained, purpose-built CDK diagram
tool that depends only on ``aws-cdk-lib``/``constructs`` (no cdk-nag) and
renders AWS-icon diagrams via the system ``dot`` binary.

How it works
------------
``cdk-dia`` reads a *synthesized* cloud assembly (``cdk.out/tree.json``) — it
does not run ``cdk synth`` itself. So this script synthesizes each diagram's
stack set in-process to a temporary ``cdk.out`` (with the Docker image asset
and helm-installer Lambda mocked, exactly like the unit tests, so no Docker
daemon or live AWS access is required) and then invokes the locked ``cdk-dia``
binary from the root npm graph against ``<cdk.out>/tree.json``.

Per-stack diagrams synthesize just the target stack (passing placeholder
strings for cross-stack inputs). ``regional-api`` also instantiates the
regional stack because its constructor consumes the regional VPC construct;
``--include`` then scopes the diagram to the regional-api stack. The full view
always includes each regional aggregation bridge; direct caller access remains
a separate policy toggle.

Output is PNG only (cdk-dia does not emit SVG). The full-architecture diagram
is rendered twice: a collapsed overview and a ``--no-collapse`` detailed view.

Usage:
    python diagrams/infra_diagrams/generate.py              # all diagrams
    python diagrams/infra_diagrams/generate.py --stack all  # all diagrams
    python diagrams/infra_diagrams/generate.py --stack global
    python diagrams/infra_diagrams/generate.py --stack api-gateway
    python diagrams/infra_diagrams/generate.py --stack regional
    python diagrams/infra_diagrams/generate.py --stack regional-api
    python diagrams/infra_diagrams/generate.py --stack monitoring
    python diagrams/infra_diagrams/generate.py --stack analytics

Prerequisites:
    * Graphviz ``dot`` on PATH (``brew install graphviz`` / ``apt-get install
      graphviz``).
    * Node.js + npm. Run the root ``npm ci`` command first so ``cdk-dia``
      executes from the committed lockfile rather than an on-demand graph.
"""

from __future__ import annotations

import argparse
import contextlib
import subprocess
import sys
import tempfile
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

# Add project root to path. This script lives at
# ``diagrams/infra_diagrams/generate.py`` so the project root is two parents
# up. The ``sys.path.insert`` lets the script be invoked standalone
# (``python diagrams/infra_diagrams/generate.py``) without a prior
# ``pip install -e .``.
_PROJECT_ROOT = Path(__file__).parent.parent.parent.resolve()
sys.path.insert(0, str(_PROJECT_ROOT))

import aws_cdk as cdk  # noqa: E402

from cli.stacks import cdk_asset_consumer  # noqa: E402
from gco.config.config_loader import ConfigLoader  # noqa: E402
from gco.stacks.analytics_stack import GCOAnalyticsStack  # noqa: E402
from gco.stacks.api_gateway_global_stack import (  # noqa: E402
    AnalyticsApiConfig,
    GCOApiGatewayGlobalStack,
)
from gco.stacks.global_stack import GCOGlobalStack  # noqa: E402
from gco.stacks.monitoring_stack import GCOMonitoringStack  # noqa: E402
from gco.stacks.regional_api_gateway_stack import GCORegionalApiGatewayStack  # noqa: E402
from gco.stacks.regional_stack import GCORegionalStack  # noqa: E402


def _locked_node_tool(package_name: str) -> Path:
    """Return a root node_modules binary or fail with install guidance."""
    binary = _PROJECT_ROOT / "node_modules" / ".bin" / package_name
    if not binary.is_file():
        raise RuntimeError(
            f"{package_name} is not installed from package-lock.json; run "
            "'npm ci --ignore-scripts --no-audit --no-fund' at the project root"
        )
    return binary


# A builder instantiates the stacks for one diagram and returns the stack ids
# to pass to ``cdk-dia --include`` (or ``None`` to diagram every stack in the
# assembly, used for the full-architecture views).
Builder = Callable[[cdk.App, ConfigLoader], "list[str] | None"]


@contextlib.contextmanager
def _mocked_regional_assets() -> Iterator[None]:
    """Mock the Docker image asset + helm-installer Lambda during synth.

    Diagram generation only needs the CloudFormation topology, not real
    container images, so we stub the Docker asset (no daemon required) and the
    helm-installer custom resource the same way the unit tests do.
    """

    def _mock_helm_installer(stack: Any) -> None:
        stack.helm_installer_lambda = MagicMock()
        stack.helm_installer_provider = MagicMock()
        stack.helm_installer_provider.service_token = (
            "arn:aws:lambda:us-east-1:123456789012:function:mock"  # nosec B106
        )
        stack.helm_installer_lambda_function_name = (
            f"gco-helm-{getattr(stack, 'deployment_region', 'us-east-1')}"
        )

    with (
        patch("gco.stacks.regional_stack.ecr_assets.DockerImageAsset") as mock_docker,
        patch.object(GCORegionalStack, "_create_helm_installer_lambda", _mock_helm_installer),
    ):
        mock_docker.return_value.image_uri = (
            "123456789012.dkr.ecr.us-east-1.amazonaws.com/test:latest"
        )
        yield


def _run_cdk_dia(
    tree_path: Path, target: Path, *, include: list[str] | None, collapse: bool
) -> None:
    """Invoke the locked ``cdk-dia`` against a synthesized ``tree.json``."""
    cmd = [
        str(_locked_node_tool("cdk-dia")),
        "--tree",
        str(tree_path),
        "--target",
        str(target),
    ]
    if not collapse:
        cmd.append("--no-collapse")
    if include:
        cmd += ["--include", *include]
    subprocess.run(cmd, check=True)  # noqa: S603 — fixed argv, no shell, paths we control
    # cdk-dia writes a Graphviz ``.dot`` sidecar next to the target. It's a
    # transient intermediate (and its AWS-icon ``image=`` refs are absolute
    # local npx-cache paths, so it isn't portable) — drop it. The PNG has the
    # icons rasterized in and is the committed artifact.
    target.with_suffix(".dot").unlink(missing_ok=True)


def _generate(
    name: str,
    title: str,
    build: Builder,
    *,
    collapse: bool = True,
    context: dict[str, Any] | None = None,
) -> None:
    """Synthesize the app built by ``build`` and render it to ``<name>.png``."""
    print(f"\n📊 Generating {title}...")
    output_dir = Path(__file__).parent
    with tempfile.TemporaryDirectory() as tmp:
        assembly_dir = Path(tmp) / "cdk.out"
        with cdk_asset_consumer(_PROJECT_ROOT):
            app = cdk.App(outdir=str(assembly_dir), context=context)
            config = ConfigLoader(app)
            with _mocked_regional_assets():
                include = build(app, config)
                app.synth()
        _run_cdk_dia(
            assembly_dir / "tree.json",
            output_dir / f"{name}.png",
            include=include,
            collapse=collapse,
        )
    print(f"   ✓ Created {name}.png")


# ---------------------------------------------------------------------------
# Per-diagram builders (mirror app.py's construction, with placeholder inputs
# for cross-stack values so each diagram stays scoped to its own stack).
# ---------------------------------------------------------------------------


def _build_global(app: cdk.App, config: ConfigLoader) -> list[str]:
    project = config.get_project_name()
    region = config.get_deployment_regions()["global"]
    GCOGlobalStack(
        app,
        f"{project}-global",
        config=config,
        env=cdk.Environment(region=region),
        description="Global resources including AWS Global Accelerator",
    )
    return [f"{project}-global"]


def _build_api_gateway(app: cdk.App, config: ConfigLoader) -> list[str]:
    project = config.get_project_name()
    region = config.get_deployment_regions()["api_gateway"]
    GCOApiGatewayGlobalStack(
        app,
        f"{project}-api-gateway",
        global_accelerator_dns="placeholder.awsglobalaccelerator.com",
        project_name=project,
        api_gateway_config=config.get_api_gateway_config(),
        registry_region=config.get_global_region(),
        certificate_regions=config.get_deployment_regions()["regional"],
        backend_tls_config=config.get_backend_tls_config(),
        env=cdk.Environment(region=region),
        description="Global API Gateway with IAM authentication",
    )
    return [f"{project}-api-gateway"]


def _build_regional(app: cdk.App, config: ConfigLoader) -> list[str]:
    project = config.get_project_name()
    region = config.get_deployment_regions()["regional"][0]
    GCORegionalStack(
        app,
        f"{project}-{region}",
        config=config,
        region=region,
        auth_secret_arn=f"arn:aws:secretsmanager:{region}:123456789012:secret:placeholder",
        env=cdk.Environment(region=region),
        description=f"Regional resources for {region} - EKS, ALB, Services",
    )
    return [f"{project}-{region}"]


def _build_regional_api(app: cdk.App, config: ConfigLoader) -> list[str]:
    project = config.get_project_name()
    region = config.get_deployment_regions()["regional"][0]
    # The regional API gateway stack needs a real VPC construct, so we
    # instantiate the regional stack (with placeholder inputs) purely to
    # supply it; ``--include`` scopes the diagram to the regional-api stack.
    regional_stack = GCORegionalStack(
        app,
        f"{project}-{region}",
        config=config,
        region=region,
        auth_secret_arn=f"arn:aws:secretsmanager:{region}:123456789012:secret:placeholder",
        env=cdk.Environment(region=region),
        description=f"Regional resources for {region}",
    )
    GCORegionalApiGatewayStack(
        app,
        f"{project}-regional-api-{region}",
        config=config,
        region=region,
        vpc=regional_stack.vpc,
        auth_secret_arn=f"arn:aws:secretsmanager:{region}:123456789012:secret:placeholder",
        aggregator_role_arn=("arn:aws:iam::123456789012:role/gco-diagram-cross-region-aggregator"),
        env=cdk.Environment(region=region),
        description=f"Regional aggregation and workload bridge for {region}",
    )
    return [f"{project}-regional-api-{region}"]


def _build_full(app: cdk.App, config: ConfigLoader) -> list[str] | None:
    """Build global, API, regional, monitoring, and enabled analytics stacks.

    Returns ``None`` so callers diagram every stack. ``monitoring`` passes its
    own ``--include`` to scope the view to the monitoring stack.
    """
    project = config.get_project_name()
    regions = config.get_deployment_regions()

    global_stack = GCOGlobalStack(
        app, f"{project}-global", config=config, env=cdk.Environment(region=regions["global"])
    )
    api_gateway_stack = GCOApiGatewayGlobalStack(
        app,
        f"{project}-api-gateway",
        global_accelerator_dns=global_stack.get_accelerator_dns_name(),
        project_name=project,
        api_gateway_config=config.get_api_gateway_config(),
        registry_region=config.get_global_region(),
        certificate_regions=regions["regional"],
        backend_tls_config=config.get_backend_tls_config(),
        env=cdk.Environment(region=regions["api_gateway"]),
    )
    api_gateway_stack.add_dependency(global_stack)

    regional_stacks = []
    for region in regions["regional"]:
        regional_stack = GCORegionalStack(
            app,
            f"{project}-{region}",
            config=config,
            region=region,
            auth_secret_arn=api_gateway_stack.secret.secret_arn,
            env=cdk.Environment(region=region),
        )
        regional_stack.add_dependency(global_stack)
        regional_stack.add_dependency(api_gateway_stack)
        regional_stacks.append(regional_stack)

        regional_api_stack = GCORegionalApiGatewayStack(
            app,
            f"{project}-regional-api-{region}",
            config=config,
            region=region,
            vpc=regional_stack.vpc,
            auth_secret_arn=api_gateway_stack.secret.secret_arn,
            aggregator_role_arn=api_gateway_stack.aggregator_role.role_arn,
            env=cdk.Environment(region=region),
        )
        regional_api_stack.add_dependency(regional_stack)

    monitoring_stack = GCOMonitoringStack(
        app,
        f"{project}-monitoring",
        config=config,
        global_stack=global_stack,
        regional_stacks=regional_stacks,
        api_gateway_stack=api_gateway_stack,
        env=cdk.Environment(region=regions["monitoring"]),
    )
    for regional_stack in regional_stacks:
        monitoring_stack.add_dependency(regional_stack)

    if config.get_analytics_enabled():
        analytics_stack = GCOAnalyticsStack(
            app,
            f"{project}-analytics",
            config=config,
            env=cdk.Environment(region=regions["api_gateway"]),
            description=(
                "Optional ML and analytics environment (SageMaker Studio, EMR Serverless, Cognito)"
            ),
        )
        analytics_stack.add_dependency(global_stack)
        api_gateway_stack.set_analytics_config(
            AnalyticsApiConfig(
                user_pool_arn=analytics_stack.cognito_pool.user_pool_arn,
                user_pool_client_id=analytics_stack.cognito_client.user_pool_client_id,
                presigned_url_lambda=analytics_stack.presigned_url_lambda,
                studio_domain_name=analytics_stack.studio_domain.domain_name or "",
                callback_url=(
                    f"https://{api_gateway_stack.api.rest_api_id}.execute-api."
                    f"{regions['api_gateway']}.amazonaws.com/prod/studio/callback"
                ),
            )
        )
        api_gateway_stack.add_dependency(analytics_stack)
    return None


def _build_monitoring(app: cdk.App, config: ConfigLoader) -> list[str]:
    _build_full(app, config)
    return [f"{config.get_project_name()}-monitoring"]


def _build_analytics(app: cdk.App, config: ConfigLoader) -> list[str]:
    project = config.get_project_name()
    region = config.get_deployment_regions()["api_gateway"]
    GCOAnalyticsStack(
        app,
        f"{project}-analytics",
        config=config,
        env=cdk.Environment(region=region),
        description=(
            "Optional ML and analytics environment (SageMaker Studio, EMR Serverless, Cognito)"
        ),
    )
    return [f"{project}-analytics"]


# Context overlay that force-enables the analytics environment (mirrors the
# overlay the property tests use), so ``ConfigLoader.get_analytics_enabled()``
# returns True during the analytics-diagram synth.
_ANALYTICS_CONTEXT: dict[str, Any] = {
    "analytics_environment": {
        "enabled": True,
        "hyperpod": {"enabled": False},
        "canvas": {"enabled": False},
        "cognito": {"domain_prefix": None, "removal_policy": "destroy"},
        "efs": {"removal_policy": "destroy"},
        "studio": {"user_profile_name_prefix": None},
    },
}


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate GCO infrastructure diagrams")
    parser.add_argument(
        "--stack",
        choices=[
            "all",
            "global",
            "api-gateway",
            "regional",
            "regional-api",
            "monitoring",
            "analytics",
        ],
        default="all",
        help="Which stack diagram to generate (default: all)",
    )
    args = parser.parse_args()

    output_dir = Path(__file__).parent
    print("🏗️  GCO Infrastructure Diagram Generator (cdk-dia)")
    print("=" * 50)

    if args.stack in ("all", "global"):
        _generate("global-stack", "GCO Global Stack - AWS Global Accelerator", _build_global)
    if args.stack in ("all", "api-gateway"):
        _generate("api-gateway-stack", "GCO API Gateway Stack", _build_api_gateway)
    if args.stack in ("all", "regional"):
        _generate("regional-stack", "GCO Regional Stack", _build_regional)
    if args.stack in ("all", "regional-api"):
        _generate("regional-api-stack", "GCO Regional API Gateway Stack", _build_regional_api)
    if args.stack in ("all", "monitoring"):
        _generate("monitoring-stack", "GCO Monitoring Stack", _build_monitoring)
    if args.stack in ("all", "analytics"):
        _generate(
            "analytics-stack",
            "GCO Analytics Stack - SageMaker Studio + EMR + Cognito",
            _build_analytics,
            context=_ANALYTICS_CONTEXT,
        )
    if args.stack == "all":
        _generate(
            "full-architecture",
            "GCO Complete Infrastructure Architecture",
            _build_full,
            context=_ANALYTICS_CONTEXT,
        )
        _generate(
            "full-architecture-detailed",
            "GCO Detailed Architecture",
            _build_full,
            collapse=False,
            context=_ANALYTICS_CONTEXT,
        )

    print("\n" + "=" * 50)
    print("✅ Diagram generation complete!")
    print(f"   Output directory: {output_dir.absolute()}")
    for f in sorted(output_dir.glob("*.png")):
        print(f"   - {f.name} ({f.stat().st_size / 1024 / 1024:.1f} MB)")


if __name__ == "__main__":
    main()
