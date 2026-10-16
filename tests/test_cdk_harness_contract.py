"""Contracts that keep the full-app CDK test harness honest.

The cdk-nag gate, the synthesis matrix, and the project-name scoping and
analytics property suites only ever check the stacks
``tests/_cdk_app_builder.py::build_all_stacks`` creates, and only the tests CI
actually selects. Both properties drifted without anyone noticing:

* The builder was copied three times. Until PR #461 none of the copies built
  ``GCORegionalApiGatewayStack``, so a same-Region synth failure in the
  bridge's auth-secret grants reached a user first. After it, the copy behind
  the scoping tests still skipped the bridge and still built the API Gateway
  stack without app.py's ``registry_region`` and certificate arguments.
* Two copies called ``GCOGlobalStack.add_regional_endpoint``, which app.py had
  stopped calling and which did nothing.
* ``unit:cdk:nag-compliance`` selects tests by node ID, and
  ``scripts/split_tests.py`` keeps the module out of the core shards, so
  ``test_no_unsuppressed_findings_with_concrete_account`` ran nowhere.

These checks are structural (AST and workflow text) apart from one mocked call,
so they run in the core suite without waiting on a synth.
"""

from __future__ import annotations

import ast
import contextlib
import re
from collections.abc import Iterator
from pathlib import Path
from unittest.mock import MagicMock

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent
APP_PY = ROOT / "app.py"
NAG_TESTS = ROOT / "tests" / "test_nag_compliance.py"
UNIT_TESTS_WORKFLOW = ROOT / ".github" / "workflows" / "unit-tests.yml"

#: Every full-app builder besides ``app.py::main`` itself, as (file, function).
#: Each must build what app.py builds, the way app.py builds it.
_APP_BUILDERS: tuple[tuple[str, str], ...] = (
    ("tests/_cdk_app_builder.py", "build_all_stacks"),
    # The infra diagrams' full-architecture and monitoring views.
    ("diagrams/infra_diagrams/generate.py", "_build_full"),
)

#: A function that constructs this many distinct ``GCO*Stack`` classes is a copy
#: of app.py's builder. Focused tests and the per-stack diagram builders
#: construct one or two.
_APP_BUILDER_MIN_STACK_CLASSES = 4

#: Where a full-app builder could hide. Generated Lambda build trees are skipped.
_SOURCE_ROOTS = ("cli", "diagrams", "gco", "gco_mcp", "lambda", "scripts", "tests")

#: Keyword arguments that do not change what a stack synthesizes for the
#: matrices: a stack's ``description`` is template prose, not resources or IAM.
_COSMETIC_KWARGS = frozenset({"description"})

_STACK_CALL = re.compile(r"\bGCO\w*Stack\(")


def _stack_class(node: ast.AST | None) -> str | None:
    """The class name when ``node`` constructs a ``GCO*Stack``, else ``None``."""
    if (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id.startswith("GCO")
        and node.func.id.endswith("Stack")
    ):
        return node.func.id
    return None


def _stack_constructor_kwargs(node: ast.AST) -> dict[str, set[str]]:
    """Map each ``GCO*Stack`` class constructed under ``node`` to its keyword names."""
    calls: dict[str, set[str]] = {}
    for call in ast.walk(node):
        stack_class = _stack_class(call)
        if stack_class is None:
            continue
        assert isinstance(call, ast.Call)
        keywords = {kw.arg for kw in call.keywords if kw.arg is not None}
        calls.setdefault(stack_class, set()).update(keywords - _COSMETIC_KWARGS)
    return calls


def _stack_calls(function: ast.AST) -> set[str]:
    """Every method called on a local bound to a new stack, as
    ``Class.method(ArgumentClass)``; the argument is named only when it is
    another such stack (a dependency edge)."""
    classes: dict[str, str] = {}
    for node in ast.walk(function):
        if isinstance(node, ast.Assign) and (stack_class := _stack_class(node.value)):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    classes[target.id] = stack_class
    calls: set[str] = set()
    for node in ast.walk(function):
        if not (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id in classes
        ):
            continue
        first = node.args[0] if node.args else None
        argument = classes.get(first.id, "") if isinstance(first, ast.Name) else ""
        calls.add(f"{classes[node.func.value.id]}.{node.func.attr}({argument})")
    return calls


def _function(path: Path, name: str) -> ast.FunctionDef:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    raise AssertionError(f"{path.relative_to(ROOT)} no longer defines {name}()")


_BUILDER_IDS = [f"{path}::{name}" for path, name in _APP_BUILDERS]


@pytest.mark.parametrize(("path", "name"), _APP_BUILDERS, ids=_BUILDER_IDS)
def test_builder_constructs_what_app_py_constructs(path: str, name: str) -> None:
    """Same stack classes, and the same constructor arguments, as ``app.py``.

    Comparing keyword names rather than values keeps this structural: a
    builder may compute an argument differently, but it may not leave one out
    and fall back to a default that production never uses.
    """
    app_stacks = _stack_constructor_kwargs(_function(APP_PY, "main"))
    builder_stacks = _stack_constructor_kwargs(_function(ROOT / path, name))

    # Sanity floor so a renamed entry point cannot make this pass vacuously.
    assert {"GCOGlobalStack", "GCORegionalStack", "GCORegionalApiGatewayStack"} <= set(app_stacks)

    missing = sorted(set(app_stacks) - set(builder_stacks))
    assert not missing, (
        f"{path}::{name} does not build these stacks app.py builds, so nothing it "
        f"feeds checks them: {missing}"
    )
    extra = sorted(set(builder_stacks) - set(app_stacks))
    assert not extra, f"{path}::{name} builds stacks app.py does not: {extra}"

    drift = {
        stack: {
            "missing": sorted(app_stacks[stack] - builder_stacks[stack]),
            "extra": sorted(builder_stacks[stack] - app_stacks[stack]),
        }
        for stack in app_stacks
        if app_stacks[stack] != builder_stacks[stack]
    }
    assert not drift, (
        f"{path}::{name} constructs these stacks with different keyword arguments "
        f"than app.py, so it checks a configuration production never deploys: {drift}"
    )


@pytest.mark.parametrize(("path", "name"), _APP_BUILDERS, ids=_BUILDER_IDS)
def test_builder_calls_what_app_py_calls_on_each_stack(path: str, name: str) -> None:
    """Same dependency edges and the same mutators, such as
    ``set_analytics_config``, as ``app.py``. A call app.py does not make adds
    resources, references or ordering production never has."""
    expected = _stack_calls(_function(APP_PY, "main"))
    actual = _stack_calls(_function(ROOT / path, name))

    assert "GCOApiGatewayGlobalStack.add_stack_dependency(GCOGlobalStack)" in expected, (
        f"sanity floor: app.py's stack calls were not found: {sorted(expected)}"
    )
    assert actual == expected, (
        f"{path}::{name} calls stack methods app.py does not, or skips ones it does.\n"
        f"  only in the builder: {sorted(actual - expected)}\n"
        f"  only in app.py:      {sorted(expected - actual)}"
    )


def _python_sources() -> Iterator[Path]:
    """Authored Python under ``_SOURCE_ROOTS``: generated ``*-build`` trees,
    installed ``node_modules`` and hidden directories (caches, virtualenvs)
    are skipped."""
    yield APP_PY
    for root in _SOURCE_ROOTS:
        for path in sorted((ROOT / root).rglob("*.py")):
            parts = path.relative_to(ROOT).parts[:-1]
            if not any(
                part.endswith("-build") or part == "node_modules" or part.startswith(".")
                for part in parts
            ):
                yield path


def test_no_other_function_builds_the_whole_app() -> None:
    """A new copy of app.py's builder must be held to app.py like the others.

    Every function constructing ``_APP_BUILDER_MIN_STACK_CLASSES`` or more stack
    classes is a full-app builder. Use ``tests._cdk_app_builder.build_all_stacks``
    instead, or register the function in ``_APP_BUILDERS``.
    """
    registered = {("app.py", "main"), *_APP_BUILDERS}
    found: set[tuple[str, str]] = set()
    for path in _python_sources():
        # The cheap text filter runs first, so only a file that names four or
        # more stack classes is ever parsed.
        text = path.read_text(encoding="utf-8", errors="replace")
        if len(set(_STACK_CALL.findall(text))) < _APP_BUILDER_MIN_STACK_CLASSES:
            continue
        for node in ast.walk(ast.parse(text)):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            classes = {name for child in ast.walk(node) if (name := _stack_class(child))}
            if len(classes) >= _APP_BUILDER_MIN_STACK_CLASSES:
                found.add((path.relative_to(ROOT).as_posix(), node.name))

    unregistered = sorted(found - registered)
    assert not unregistered, (
        "these functions build the whole app outside tests._cdk_app_builder, so "
        "nothing holds them to app.py; call build_all_stacks or add them to "
        f"_APP_BUILDERS: {unregistered}"
    )
    gone = sorted(registered - found)
    assert not gone, f"registered builders that no longer build the whole app: {gone}"


def test_synthesize_app_holds_the_assets_and_stands_in_images_for_the_synth(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``synthesize_app`` builds and synthesizes inside both the build-directory
    lock and the Docker image stand-in, and leaves the real class in place."""
    from aws_cdk import aws_ecr_assets

    import cli.stacks
    from tests import _cdk_app_builder as builder

    real_image_asset = aws_ecr_assets.DockerImageAsset
    events: list[object] = []

    @contextlib.contextmanager
    def recording_consumer(project_root: Path) -> Iterator[None]:
        events.append(("lock", Path(project_root)))
        yield
        events.append("unlock")

    def recording_build(app: object, account: str | None = None) -> None:
        events.append(("build", account))

    def recording_synth() -> str:
        events.append(("synth", aws_ecr_assets.DockerImageAsset is real_image_asset))
        return "assembly"

    monkeypatch.setattr(cli.stacks, "cdk_asset_consumer", recording_consumer)
    monkeypatch.setattr(builder, "build_all_stacks", recording_build)
    app = MagicMock()
    app.synth.side_effect = recording_synth

    assert builder.synthesize_app(app, account="123456789012") == "assembly"
    assert events == [
        ("lock", builder.PROJECT_ROOT),
        ("build", "123456789012"),
        ("synth", False),
        "unlock",
    ]
    assert aws_ecr_assets.DockerImageAsset is real_image_asset


def _nag_compliance_test_names() -> list[str]:
    tree = ast.parse(NAG_TESTS.read_text(encoding="utf-8"))
    names: list[str] = []
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name.startswith("test_"):
            names.append(node.name)
        elif isinstance(node, ast.ClassDef) and node.name.startswith("Test"):
            names.extend(
                item.name
                for item in node.body
                if isinstance(item, ast.FunctionDef) and item.name.startswith("test_")
            )
    return names


def _nag_job_run_scripts() -> str:
    workflow = yaml.safe_load(UNIT_TESTS_WORKFLOW.read_text(encoding="utf-8"))
    for job in workflow["jobs"].values():
        if str(job.get("name", "")).startswith("unit:cdk:nag-compliance ("):
            return "\n".join(str(step.get("run", "")) for step in job.get("steps", []))
    raise AssertionError("unit-tests.yml no longer has a unit:cdk:nag-compliance matrix job")


def test_every_nag_compliance_test_is_selected_by_ci() -> None:
    """Each test in ``tests/test_nag_compliance.py`` must be named by the CI job.

    The job selects node IDs explicitly, and the core shards ignore the
    module, so a test nobody adds to the job's ``pytest`` invocation never runs.
    """
    names = _nag_compliance_test_names()
    assert len(names) >= 2, f"sanity floor: only discovered {names}"

    scripts = _nag_job_run_scripts()
    # The name must end at a non-identifier character, so selecting
    # ``test_no_unsuppressed_findings_with_concrete_account`` does not also
    # count as selecting ``test_no_unsuppressed_findings``.
    unselected = [
        name for name in names if not re.search(rf"::{re.escape(name)}(?![A-Za-z0-9_])", scripts)
    ]
    assert not unselected, (
        "unit:cdk:nag-compliance never runs these tests from tests/test_nag_compliance.py; "
        f"add their node IDs to the job's pytest invocation: {unselected}"
    )
