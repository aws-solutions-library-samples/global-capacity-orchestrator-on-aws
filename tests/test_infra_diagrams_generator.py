"""Offline topology contracts for the infrastructure diagram generator.

Covers ``diagrams/infra_diagrams/generate.py``: every per-stack builder stays
scoped to the stack it diagrams, ``_generate`` synthesizes into a scratch
assembly that is gone once cdk-dia has consumed it, ``_run_cdk_dia`` runs the
locked ``node_modules`` binary with an exact argv and drops the ``.dot``
sidecar, and ``main`` dispatches ``--stack`` choices and prunes stale artifacts
only for a full run. No cdk-dia, dot, or npm process is ever launched and
nothing is written outside ``tmp_path``.
"""

import json
import subprocess
import sys
from pathlib import Path

import aws_cdk as cdk
import pytest
from aws_cdk import aws_ecr_assets

from diagrams.infra_diagrams import generate
from diagrams.infra_diagrams._catalog import INFRA_DIAGRAM_NAMES
from gco.config.config_loader import ConfigLoader

ROOT = Path(__file__).resolve().parents[1]


def test_regional_diagram_keeps_helm_convergence_topology(tmp_path: Path, monkeypatch) -> None:
    """Asset stubs must not erase deployable Lambda/Step Functions resources."""
    monkeypatch.chdir(ROOT)
    app = cdk.App(outdir=str(tmp_path / "cdk.out"))
    config = ConfigLoader(app)
    with generate._mocked_regional_assets():
        stack_names = generate._build_regional(app, config)
        assembly = app.synth()

    template = assembly.get_stack_by_name(stack_names[0]).template
    logical_ids = set(template["Resources"])
    required_prefixes = {
        "HelmInstallerFunction",
        "HelmInstallStateMachine",
        "HelmOrchestratorOnEvent",
        "HelmInstallerProvider",
    }
    for prefix in required_prefixes:
        assert any(logical_id.startswith(prefix) for logical_id in logical_ids), prefix


def test_full_architecture_keeps_every_stack_bridge_and_helm_topology(
    tmp_path: Path, monkeypatch
) -> None:
    """Aggregate views must retain stacks and regional convergence resources."""
    monkeypatch.chdir(ROOT)
    app = cdk.App(
        outdir=str(tmp_path / "cdk.out"),
        context=generate._ANALYTICS_CONTEXT,
    )
    config = ConfigLoader(app)
    project = config.get_project_name()
    regions = config.get_deployment_regions()
    with generate._mocked_regional_assets():
        assert generate._build_full(app, config) is None
        assembly = app.synth()

    expected_stacks = {
        f"{project}-global",
        f"{project}-api-gateway",
        f"{project}-monitoring",
        f"{project}-analytics",
        *(f"{project}-{region}" for region in regions["regional"]),
        *(f"{project}-regional-api-{region}" for region in regions["regional"]),
    }
    actual_stacks = {artifact.stack_name for artifact in assembly.stacks}
    assert actual_stacks >= expected_stacks

    required_helm_prefixes = {
        "HelmInstallerFunction",
        "HelmInstallStateMachine",
        "HelmOrchestratorOnEvent",
        "HelmInstallerProvider",
    }
    for region in regions["regional"]:
        regional = assembly.get_stack_by_name(f"{project}-{region}").template
        logical_ids = set(regional["Resources"])
        for prefix in required_helm_prefixes:
            assert any(logical_id.startswith(prefix) for logical_id in logical_ids), (
                region,
                prefix,
            )
        regional_api = assembly.get_stack_by_name(f"{project}-regional-api-{region}").template
        assert any(
            resource["Type"] == "AWS::Lambda::Function"
            for resource in regional_api["Resources"].values()
        ), region


# ---------------------------------------------------------------------------
# Per-stack builders: construct only (no synth) and pin the scoping contract.
# ---------------------------------------------------------------------------


def _stacks_by_id(app: cdk.App) -> dict[str, cdk.Stack]:
    """Return the top-level stacks of ``app`` keyed by construct id."""
    return {child.node.id: child for child in app.node.children if isinstance(child, cdk.Stack)}


def _explicit_dependencies(stack: cdk.Stack) -> set[str]:
    """Return the ids of the stacks ``stack`` was made to depend on."""
    return {dependency.node.id for dependency in stack.dependencies}


def test_build_global_constructs_only_the_global_stack(tmp_path: Path, monkeypatch) -> None:
    """The global diagram holds exactly the global stack, in the global region."""
    monkeypatch.chdir(ROOT)
    app = cdk.App(outdir=str(tmp_path / "cdk.out"))
    config = ConfigLoader(app)
    project = config.get_project_name()
    regions = config.get_deployment_regions()

    include = generate._build_global(app, config)

    stacks = _stacks_by_id(app)
    assert include == [f"{project}-global"]
    assert set(stacks) == {f"{project}-global"}
    global_stack = stacks[f"{project}-global"]
    assert global_stack.region == regions["global"]
    assert (
        global_stack.template_options.description
        == "Global resources including AWS Global Accelerator"
    )


def test_build_api_gateway_constructs_only_the_api_gateway_stack(
    tmp_path: Path, monkeypatch
) -> None:
    """The API Gateway diagram holds one stack, placed in the api_gateway region."""
    monkeypatch.chdir(ROOT)
    app = cdk.App(outdir=str(tmp_path / "cdk.out"))
    config = ConfigLoader(app)
    project = config.get_project_name()
    regions = config.get_deployment_regions()

    include = generate._build_api_gateway(app, config)

    stacks = _stacks_by_id(app)
    assert include == [f"{project}-api-gateway"]
    assert set(stacks) == {f"{project}-api-gateway"}
    api_stack = stacks[f"{project}-api-gateway"]
    assert api_stack.region == regions["api_gateway"]
    assert api_stack.template_options.description == "Global API Gateway with IAM authentication"


def test_build_regional_api_borrows_the_regional_vpc_but_includes_only_the_bridge(
    tmp_path: Path, monkeypatch
) -> None:
    """regional-api instantiates the regional stack for its VPC yet scopes --include to the bridge."""
    monkeypatch.chdir(ROOT)
    app = cdk.App(outdir=str(tmp_path / "cdk.out"))
    config = ConfigLoader(app)
    project = config.get_project_name()
    region = config.get_deployment_regions()["regional"][0]

    with generate._mocked_regional_assets():
        include = generate._build_regional_api(app, config)

    stacks = _stacks_by_id(app)
    assert include == [f"{project}-regional-api-{region}"]
    assert set(stacks) == {f"{project}-{region}", f"{project}-regional-api-{region}"}
    regional = stacks[f"{project}-{region}"]
    bridge = stacks[f"{project}-regional-api-{region}"]
    assert bridge.region == region
    assert bridge.vpc is regional.vpc
    assert regional.template_options.description == f"Regional resources for {region}"
    assert (
        bridge.template_options.description
        == f"Regional aggregation and workload bridge for {region}"
    )


def test_build_monitoring_builds_the_full_topology_without_optional_analytics(
    tmp_path: Path, monkeypatch
) -> None:
    """With analytics off (the default) the full build skips analytics; monitoring scopes to itself."""
    monkeypatch.chdir(ROOT)
    app = cdk.App(outdir=str(tmp_path / "cdk.out"))
    config = ConfigLoader(app)
    assert config.get_analytics_enabled() is False
    project = config.get_project_name()
    regions = config.get_deployment_regions()

    with generate._mocked_regional_assets():
        include = generate._build_monitoring(app, config)

    stacks = _stacks_by_id(app)
    assert include == [f"{project}-monitoring"]
    assert set(stacks) == {
        f"{project}-global",
        f"{project}-api-gateway",
        f"{project}-monitoring",
        *(f"{project}-{region}" for region in regions["regional"]),
        *(f"{project}-regional-api-{region}" for region in regions["regional"]),
    }
    assert stacks[f"{project}-monitoring"].region == regions["monitoring"]

    api_dependencies = _explicit_dependencies(stacks[f"{project}-api-gateway"])
    assert f"{project}-global" in api_dependencies
    assert f"{project}-analytics" not in api_dependencies
    for region in regions["regional"]:
        assert _explicit_dependencies(stacks[f"{project}-{region}"]) >= {
            f"{project}-global",
            f"{project}-api-gateway",
        }
        assert _explicit_dependencies(stacks[f"{project}-regional-api-{region}"]) >= {
            f"{project}-{region}"
        }
    assert _explicit_dependencies(stacks[f"{project}-monitoring"]) >= {
        f"{project}-{region}" for region in regions["regional"]
    }


def test_build_analytics_constructs_only_the_analytics_stack(tmp_path: Path, monkeypatch) -> None:
    """The analytics diagram holds one stack, co-located with the API Gateway region."""
    monkeypatch.chdir(ROOT)
    app = cdk.App(outdir=str(tmp_path / "cdk.out"), context=generate._ANALYTICS_CONTEXT)
    config = ConfigLoader(app)
    assert config.get_analytics_enabled() is True
    project = config.get_project_name()
    regions = config.get_deployment_regions()

    include = generate._build_analytics(app, config)

    stacks = _stacks_by_id(app)
    assert include == [f"{project}-analytics"]
    assert set(stacks) == {f"{project}-analytics"}
    analytics = stacks[f"{project}-analytics"]
    assert analytics.region == regions["api_gateway"]
    assert analytics.template_options.description == (
        "Optional ML and analytics environment (SageMaker Studio, EMR Serverless, Cognito)"
    )


# ---------------------------------------------------------------------------
# cdk-dia invocation: locked binary lookup, exact argv, sidecar cleanup.
# ---------------------------------------------------------------------------


def _install_locked_cdk_dia(tmp_path: Path, monkeypatch) -> Path:
    """Point the generator at a fake project root that carries a locked cdk-dia."""
    root = tmp_path / "project"
    binary = root / "node_modules" / ".bin" / "cdk-dia"
    binary.parent.mkdir(parents=True)
    binary.write_text("#!/bin/sh\nexit 0\n")
    monkeypatch.setattr(generate, "_PROJECT_ROOT", root)
    return binary


class _FakeCdkDia:
    """Stand-in for ``subprocess.run`` that mimics what cdk-dia leaves on disk."""

    def __init__(self, *, sidecar: bool, returncode: int = 0) -> None:
        self.sidecar = sidecar
        self.returncode = returncode
        self.calls: list[tuple[list[str], dict]] = []

    def __call__(self, cmd, **kwargs):
        self.calls.append((list(cmd), kwargs))
        if kwargs.get("check") and self.returncode:
            raise subprocess.CalledProcessError(self.returncode, cmd)
        target = Path(cmd[cmd.index("--target") + 1])
        target.write_bytes(b"\x89PNG\r\n\x1a\n")
        if self.sidecar:
            target.with_suffix(".dot").write_text("digraph G {}\n")
        return subprocess.CompletedProcess(cmd, self.returncode)


def test_locked_node_tool_returns_the_root_node_modules_binary(tmp_path: Path, monkeypatch) -> None:
    """The locked binary is resolved from <project root>/node_modules/.bin."""
    binary = _install_locked_cdk_dia(tmp_path, monkeypatch)
    assert generate._locked_node_tool("cdk-dia") == binary


@pytest.mark.parametrize("layout", ["absent", "directory"])
def test_locked_node_tool_demands_npm_ci_when_binary_is_missing(
    tmp_path: Path, monkeypatch, layout: str
) -> None:
    """Without a locked binary the generator refuses to run and points at npm ci."""
    root = tmp_path / "project"
    if layout == "directory":
        (root / "node_modules" / ".bin" / "cdk-dia").mkdir(parents=True)
    monkeypatch.setattr(generate, "_PROJECT_ROOT", root)
    with pytest.raises(
        RuntimeError,
        match=(
            r"cdk-dia is not installed from package-lock\.json; run "
            r"'npm ci --ignore-scripts --no-audit --no-fund' at the project root"
        ),
    ):
        generate._locked_node_tool("cdk-dia")


def test_run_cdk_dia_scopes_with_include_and_drops_the_dot_sidecar(
    tmp_path: Path, monkeypatch
) -> None:
    """A collapsed, scoped render passes --include and leaves only the PNG behind."""
    binary = _install_locked_cdk_dia(tmp_path, monkeypatch)
    fake = _FakeCdkDia(sidecar=True)
    monkeypatch.setattr(generate.subprocess, "run", fake)
    tree = tmp_path / "cdk.out" / "tree.json"
    target = tmp_path / "out" / "global-stack.png"
    target.parent.mkdir()

    generate._run_cdk_dia(tree, target, include=["gco-global", "gco-api-gateway"], collapse=True)

    assert fake.calls == [
        (
            [
                str(binary),
                "--tree",
                str(tree),
                "--target",
                str(target),
                "--include",
                "gco-global",
                "gco-api-gateway",
            ],
            {"check": True},
        )
    ]
    assert target.is_file()
    assert not target.with_suffix(".dot").exists()


@pytest.mark.parametrize("include", [None, []])
def test_run_cdk_dia_detailed_view_disables_collapse_and_tolerates_no_sidecar(
    tmp_path: Path, monkeypatch, include
) -> None:
    """Unscoped detailed renders pass --no-collapse and no --include; a missing .dot is fine."""
    binary = _install_locked_cdk_dia(tmp_path, monkeypatch)
    fake = _FakeCdkDia(sidecar=False)
    monkeypatch.setattr(generate.subprocess, "run", fake)
    tree = tmp_path / "cdk.out" / "tree.json"
    target = tmp_path / "out" / "full-architecture-detailed.png"
    target.parent.mkdir()

    generate._run_cdk_dia(tree, target, include=include, collapse=False)

    assert fake.calls == [
        (
            [str(binary), "--tree", str(tree), "--target", str(target), "--no-collapse"],
            {"check": True},
        )
    ]
    assert target.is_file()
    assert sorted(path.name for path in target.parent.iterdir()) == [target.name]


def test_run_cdk_dia_propagates_render_failures(tmp_path: Path, monkeypatch) -> None:
    """A non-zero cdk-dia exit aborts the run instead of reporting a diagram."""
    _install_locked_cdk_dia(tmp_path, monkeypatch)
    fake = _FakeCdkDia(sidecar=False, returncode=3)
    monkeypatch.setattr(generate.subprocess, "run", fake)
    target = tmp_path / "out" / "x.png"
    target.parent.mkdir()

    with pytest.raises(subprocess.CalledProcessError):
        generate._run_cdk_dia(tmp_path / "tree.json", target, include=None, collapse=True)
    assert len(fake.calls) == 1


def test_run_cdk_dia_refuses_to_render_without_the_locked_binary(
    tmp_path: Path, monkeypatch
) -> None:
    """No on-demand npx fallback: a missing locked cdk-dia fails before any process starts."""
    monkeypatch.setattr(generate, "_PROJECT_ROOT", tmp_path / "project")
    fake = _FakeCdkDia(sidecar=False)
    monkeypatch.setattr(generate.subprocess, "run", fake)

    with pytest.raises(RuntimeError, match="npm ci"):
        generate._run_cdk_dia(
            tmp_path / "tree.json", tmp_path / "x.png", include=None, collapse=True
        )
    assert fake.calls == []


# ---------------------------------------------------------------------------
# _generate: synth into a scratch assembly, hand tree.json to cdk-dia, clean up.
# ---------------------------------------------------------------------------


@pytest.fixture
def fake_output_dir(tmp_path: Path, monkeypatch) -> Path:
    """Redirect ``Path(__file__).parent`` in the generator to a scratch directory."""
    output_dir = tmp_path / "infra_diagrams"
    output_dir.mkdir()
    monkeypatch.setattr(generate, "__file__", str(output_dir / "generate.py"))
    return output_dir


class _RenderRecorder:
    """Replacement for ``_run_cdk_dia`` that snapshots the assembly it was handed."""

    def __init__(self) -> None:
        self.calls: list[dict] = []

    def __call__(self, tree_path: Path, target: Path, *, include, collapse) -> None:
        tree = json.loads(tree_path.read_text())
        self.calls.append(
            {
                "tree_path": tree_path,
                "stacks": set(tree["tree"]["children"]),
                "target": target,
                "include": include,
                "collapse": collapse,
            }
        )


def test_generate_synthesizes_a_scratch_assembly_and_renders_it(
    fake_output_dir: Path, monkeypatch, capsys
) -> None:
    """_generate synths the builder's stacks to a temp cdk.out, renders it, then discards it."""
    recorder = _RenderRecorder()
    monkeypatch.setattr(generate, "_run_cdk_dia", recorder)
    real_docker_asset = aws_ecr_assets.DockerImageAsset
    seen: dict[str, object] = {}

    def build(app: cdk.App, config: ConfigLoader) -> list[str]:
        cdk.Stack(app, "tiny")
        seen["analytics"] = config.get_analytics_enabled()
        seen["marker"] = app.node.try_get_context("marker")
        # Docker image assets are stubbed for the duration of the build.
        seen["image_uri"] = aws_ecr_assets.DockerImageAsset(app, "probe", directory=".").image_uri
        return ["tiny"]

    generate._generate("tiny-diagram", "Tiny Diagram", build)

    assert seen == {
        "analytics": False,
        "marker": None,
        "image_uri": "123456789012.dkr.ecr.us-east-1.amazonaws.com/test:latest",
    }
    assert aws_ecr_assets.DockerImageAsset is real_docker_asset
    (call,) = recorder.calls
    assert call["tree_path"].name == "tree.json"
    assert call["tree_path"].parent.name == "cdk.out"
    assert not call["tree_path"].parent.exists()
    assert not call["tree_path"].is_relative_to(fake_output_dir)
    assert "tiny" in call["stacks"]
    assert call["target"] == fake_output_dir / "tiny-diagram.png"
    assert call["include"] == ["tiny"]
    assert call["collapse"] is True
    out = capsys.readouterr().out
    assert "📊 Generating Tiny Diagram..." in out
    assert "✓ Created tiny-diagram.png" in out


def test_generate_forwards_context_and_collapse_to_the_synth_and_render(
    fake_output_dir: Path, monkeypatch
) -> None:
    """The context overlay reaches ConfigLoader and collapse/include reach cdk-dia unchanged."""
    recorder = _RenderRecorder()
    monkeypatch.setattr(generate, "_run_cdk_dia", recorder)
    seen: dict[str, object] = {}

    def build(app: cdk.App, config: ConfigLoader) -> None:
        cdk.Stack(app, "everything")
        seen["analytics"] = config.get_analytics_enabled()
        seen["marker"] = app.node.try_get_context("marker")
        return None

    generate._generate(
        "full-architecture-detailed",
        "Detailed",
        build,
        collapse=False,
        context={**generate._ANALYTICS_CONTEXT, "marker": "on"},
    )

    assert seen == {"analytics": True, "marker": "on"}
    (call,) = recorder.calls
    assert "everything" in call["stacks"]
    assert call["target"] == fake_output_dir / "full-architecture-detailed.png"
    assert call["include"] is None
    assert call["collapse"] is False


# ---------------------------------------------------------------------------
# main(): --stack dispatch table, stale-artifact pruning, final listing.
# ---------------------------------------------------------------------------


class _GenerateRecorder:
    """Replacement for ``_generate`` that records calls and leaves the PNG it would render."""

    def __init__(self, output_dir: Path) -> None:
        self.output_dir = output_dir
        self.calls: list[tuple] = []

    def __call__(self, name, title, build, *, collapse=True, context=None) -> None:
        self.calls.append((name, title, build, collapse, context))
        (self.output_dir / f"{name}.png").write_bytes(b"\x89PNG")


_ANALYTICS = generate._ANALYTICS_CONTEXT
_FULL_DISPATCH = [
    (
        "global-stack",
        "GCO Global Stack - AWS Global Accelerator",
        generate._build_global,
        True,
        None,
    ),
    ("api-gateway-stack", "GCO API Gateway Stack", generate._build_api_gateway, True, None),
    ("regional-stack", "GCO Regional Stack", generate._build_regional, True, None),
    (
        "regional-api-stack",
        "GCO Regional API Gateway Stack",
        generate._build_regional_api,
        True,
        None,
    ),
    ("monitoring-stack", "GCO Monitoring Stack", generate._build_monitoring, True, None),
    (
        "analytics-stack",
        "GCO Analytics Stack - SageMaker Studio + EMR + Cognito",
        generate._build_analytics,
        True,
        _ANALYTICS,
    ),
    (
        "full-architecture",
        "GCO Complete Infrastructure Architecture",
        generate._build_full,
        True,
        _ANALYTICS,
    ),
    (
        "full-architecture-detailed",
        "GCO Detailed Architecture",
        generate._build_full,
        False,
        _ANALYTICS,
    ),
]


@pytest.mark.parametrize("argv", [[], ["--stack", "all"]], ids=["default", "explicit-all"])
def test_main_full_run_renders_every_diagram_then_prunes_stale_artifacts(
    fake_output_dir: Path, monkeypatch, capsys, argv: list[str]
) -> None:
    """A full run renders the eight catalogued diagrams in order, then removes stray PNG/DOT files."""
    recorder = _GenerateRecorder(fake_output_dir)
    monkeypatch.setattr(generate, "_generate", recorder)
    monkeypatch.setattr(sys, "argv", ["generate.py", *argv])
    (fake_output_dir / "stale.png").write_bytes(b"obsolete")
    (fake_output_dir / "full-architecture.dot").write_text("digraph G {}\n")

    generate.main()

    assert recorder.calls == _FULL_DISPATCH
    assert [call[0] for call in recorder.calls] == list(INFRA_DIAGRAM_NAMES)
    assert {path.name for path in fake_output_dir.iterdir()} == {
        f"{name}.png" for name in INFRA_DIAGRAM_NAMES
    }
    out = capsys.readouterr().out
    assert "🧹 Removed obsolete stale.png" in out
    assert "🧹 Removed transient full-architecture.dot" in out
    assert "✅ Diagram generation complete!" in out
    assert f"Output directory: {fake_output_dir.absolute()}" in out
    listing = [line for line in out.splitlines() if line.startswith("   - ")]
    # The listing is sorted by file name, so ``full-architecture-detailed.png``
    # precedes ``full-architecture.png`` ('-' sorts before '.').
    expected_files = sorted(f"{name}.png" for name in INFRA_DIAGRAM_NAMES)
    assert listing == [f"   - {file_name} (0.0 MB)" for file_name in expected_files]


@pytest.mark.parametrize(
    ("choice", "expected"),
    [(entry[0].removesuffix("-stack"), entry) for entry in _FULL_DISPATCH[:6]],
    ids=[entry[0].removesuffix("-stack") for entry in _FULL_DISPATCH[:6]],
)
def test_main_single_stack_renders_only_that_diagram_without_pruning(
    fake_output_dir: Path, monkeypatch, capsys, choice: str, expected: tuple
) -> None:
    """--stack <one> renders exactly that diagram and leaves other artifacts untouched."""
    recorder = _GenerateRecorder(fake_output_dir)
    monkeypatch.setattr(generate, "_generate", recorder)
    monkeypatch.setattr(sys, "argv", ["generate.py", "--stack", choice])
    (fake_output_dir / "stale.png").write_bytes(b"obsolete")
    (fake_output_dir / "leftover.dot").write_text("digraph G {}\n")

    generate.main()

    assert recorder.calls == [expected]
    assert {path.name for path in fake_output_dir.iterdir()} == {
        f"{expected[0]}.png",
        "stale.png",
        "leftover.dot",
    }
    out = capsys.readouterr().out
    assert "🧹" not in out
    listing = [line for line in out.splitlines() if line.startswith("   - ")]
    assert listing == [f"   - {expected[0]}.png (0.0 MB)", "   - stale.png (0.0 MB)"]


def test_main_rejects_an_unknown_stack_choice(fake_output_dir: Path, monkeypatch, capsys) -> None:
    """An unlisted --stack value is an argparse usage error (exit 2) and renders nothing."""
    recorder = _GenerateRecorder(fake_output_dir)
    monkeypatch.setattr(generate, "_generate", recorder)
    monkeypatch.setattr(sys, "argv", ["generate.py", "--stack", "bogus"])

    with pytest.raises(SystemExit) as excinfo:
        generate.main()

    assert excinfo.value.code == 2
    assert "invalid choice: 'bogus'" in capsys.readouterr().err
    assert recorder.calls == []
    assert list(fake_output_dir.iterdir()) == []
