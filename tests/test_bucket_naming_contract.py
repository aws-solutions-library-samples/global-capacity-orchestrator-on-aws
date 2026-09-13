"""
Static contract for the S3 bucket naming policy (ADR-0005).

S3 bucket names live in one global namespace and a deleted name is not reliably
reusable, so no GCO stack may pin a physical bucket name: every bucket takes the
name CloudFormation generates and the owning stack publishes its identity to
SSM. These tests read the stack *sources* rather than synthesizing, so they run
in milliseconds and fail the moment someone adds ``bucket_name=`` to any
``s3.Bucket`` (or a ``BucketName`` to a raw ``CfnBucket``), reconstructs a name
from project/account/region, or drops the published identity contract the
consumers depend on.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
STACKS_DIR = REPO_ROOT / "gco" / "stacks"
STACK_SOURCES = sorted(STACKS_DIR.glob("*.py"))

#: Every SSM identity contract a bucket owner must publish (prefix helper name,
#: owning stack module). The three suffixes are the shared shape every consumer
#: reads.
PUBLISHED_IDENTITIES = {
    "cluster_shared_ssm_parameter_prefix": "global_stack.py",
    "regional_shared_ssm_parameter_prefix": "regional_stack.py",
    "cost_report_ssm_parameter_prefix": "monitoring_stack.py",
}
IDENTITY_SUFFIXES = ("/name", "/arn", "/region")


def _bucket_constructor_calls(tree: ast.AST) -> list[ast.Call]:
    """Every ``s3.Bucket(...)`` / ``Bucket(...)`` / ``CfnBucket(...)`` call."""
    calls: list[ast.Call] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
        if name in {"Bucket", "CfnBucket"}:
            calls.append(node)
    return calls


@pytest.mark.parametrize("source", STACK_SOURCES, ids=lambda path: path.name)
def test_no_stack_pins_a_bucket_name(source: Path) -> None:
    tree = ast.parse(source.read_text(encoding="utf-8"), filename=str(source))
    offenders = [
        f"{source.name}:{call.lineno}"
        for call in _bucket_constructor_calls(tree)
        for keyword in call.keywords
        if keyword.arg in {"bucket_name", "bucketName"}
    ]
    assert offenders == [], (
        "Explicit bucket names are a reuse hazard in S3's global namespace (ADR-0005); "
        f"let CloudFormation name these buckets: {offenders}"
    )


def test_bucket_constructions_exist_to_be_governed() -> None:
    """Guard the guard: the walk above must actually see the project's buckets."""
    total = sum(
        len(_bucket_constructor_calls(ast.parse(source.read_text(encoding="utf-8"))))
        for source in STACK_SOURCES
    )
    assert total >= 10, f"expected at least the ten known bucket constructions, saw {total}"


@pytest.mark.parametrize("source", STACK_SOURCES, ids=lambda path: path.name)
def test_no_stack_reconstructs_a_bucket_name(source: Path) -> None:
    """No f-string may assemble ``<project>-<bucket>-<account>-<region>``.

    The historic shapes were ``{prefix}-{self.account}-{self.region}`` and
    ``{project_name}-cost-reports-{account}-{region}``; catching the
    ``-{...account}-{...region}`` tail keeps any revival out of the stacks.
    """
    text = source.read_text(encoding="utf-8")
    pattern = re.compile(r"-\{[^}]*account\}-\{[^}]*region\}", re.IGNORECASE)
    hits = [
        f"{source.name}:{index}"
        for index, line in enumerate(text.splitlines(), start=1)
        if pattern.search(line) and "s3:::" not in line and "parameter" not in line
    ]
    assert hits == [], f"bucket-name reconstruction pattern found: {hits}"


def test_constants_expose_no_bucket_name_helper() -> None:
    import gco.stacks.constants as constants

    helpers = [
        name
        for name in dir(constants)
        if name.endswith("_bucket_name") or name.endswith("_bucket_name_prefix")
    ]
    assert helpers == [], f"bucket-name helpers must not exist: {helpers}"


@pytest.mark.parametrize(("helper", "owner"), sorted(PUBLISHED_IDENTITIES.items()))
def test_each_bucket_owner_publishes_the_full_identity(helper: str, owner: str) -> None:
    """The owning stack writes ``<prefix>/name``, ``/arn`` and ``/region``."""
    import gco.stacks.constants as constants

    assert callable(getattr(constants, helper))
    text = (STACKS_DIR / owner).read_text(encoding="utf-8")
    assert helper in text, f"{owner} does not use {helper}"
    # Each parameter name is an f-string ending in the literal suffix, e.g.
    # ``parameter_name=f"{cost_report_prefix}/name"``.
    missing = [suffix for suffix in IDENTITY_SUFFIXES if f'{suffix}"' not in text]
    assert missing == [], f"{owner} does not publish {missing} under {helper}"
