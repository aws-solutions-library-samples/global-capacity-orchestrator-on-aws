"""Contracts that keep the full-app CDK test harness honest.

The cdk-nag and synthesis matrices (``tests/test_nag_compliance.py`` and
``tests/test_cdk_synthesis_matrix.py``) only ever check the stacks their
shared builder, ``tests.test_nag_compliance._build_all_stacks``, creates, and
only the tests CI actually selects. Both properties drifted without anyone
noticing:

* The builder never constructed ``GCORegionalApiGatewayStack``, so the
  regional API bridge's IAM never went through cdk-nag in the matrix. A
  same-Region synth failure in its auth-secret grants reached a user first
  (PR #461). Its API Gateway stack also lacked app.py's ``registry_region``
  and certificate arguments, which only coincided with production while every
  matrix entry kept the API Gateway in the global Region.
* ``unit:cdk:nag-compliance`` selects tests by node ID, and
  ``scripts/split_tests.py`` keeps the module out of the core shards, so
  ``test_no_unsuppressed_findings_with_concrete_account`` ran nowhere.

These checks are structural (AST and workflow text), so they run in the core
suite in milliseconds rather than waiting on a synth.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
APP_PY = ROOT / "app.py"
NAG_TESTS = ROOT / "tests" / "test_nag_compliance.py"
UNIT_TESTS_WORKFLOW = ROOT / ".github" / "workflows" / "unit-tests.yml"

#: Keyword arguments that do not change what a stack synthesizes for the
#: matrices: a stack's ``description`` is template prose, not resources or IAM.
_COSMETIC_KWARGS = frozenset({"description"})


def _stack_constructor_kwargs(node: ast.AST) -> dict[str, set[str]]:
    """Map each ``GCO*Stack`` class constructed under ``node`` to its keyword names."""
    calls: dict[str, set[str]] = {}
    for call in ast.walk(node):
        if not (
            isinstance(call, ast.Call)
            and isinstance(call.func, ast.Name)
            and call.func.id.startswith("GCO")
            and call.func.id.endswith("Stack")
        ):
            continue
        keywords = {kw.arg for kw in call.keywords if kw.arg is not None}
        calls.setdefault(call.func.id, set()).update(keywords - _COSMETIC_KWARGS)
    return calls


def _function(path: Path, name: str) -> ast.FunctionDef:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    raise AssertionError(f"{path.name} no longer defines {name}()")


def test_shared_builder_constructs_every_stack_app_py_constructs() -> None:
    """Same stack classes, and the same constructor arguments, as ``app.py``.

    Comparing keyword names rather than values keeps this structural: the
    builder may compute an argument differently, but it may not leave one out
    and fall back to a default that production never uses.
    """
    app_stacks = _stack_constructor_kwargs(_function(APP_PY, "main"))
    builder_stacks = _stack_constructor_kwargs(_function(NAG_TESTS, "_build_all_stacks"))

    # Sanity floor so a renamed entry point cannot make this pass vacuously.
    assert {"GCOGlobalStack", "GCORegionalStack", "GCORegionalApiGatewayStack"} <= set(app_stacks)

    missing = sorted(set(app_stacks) - set(builder_stacks))
    assert not missing, (
        "tests/test_nag_compliance.py::_build_all_stacks does not build these stacks "
        f"app.py builds, so the nag and synthesis matrices never check them: {missing}"
    )
    extra = sorted(set(builder_stacks) - set(app_stacks))
    assert not extra, f"_build_all_stacks builds stacks app.py does not: {extra}"

    drift = {
        stack: {
            "missing": sorted(app_stacks[stack] - builder_stacks[stack]),
            "extra": sorted(builder_stacks[stack] - app_stacks[stack]),
        }
        for stack in app_stacks
        if app_stacks[stack] != builder_stacks[stack]
    }
    assert not drift, (
        "_build_all_stacks constructs these stacks with different keyword arguments "
        f"than app.py, so the matrices check a configuration production never deploys: {drift}"
    )


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
