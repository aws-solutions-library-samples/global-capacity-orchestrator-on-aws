"""
Guard against "dead config" in cdk.json.

This test scans every dict block under cdk.json `context` and confirms that
each configured key is actually referenced in the project's Python source
(under gco/ and lambda/). If a key exists in cdk.json but never appears as a
dict-literal, attribute access, or .get() call anywhere in the code, the
test fails with a pointer to either consume the value or remove it.

Discovery is automatic: any new top-level dict block under `context` gets
covered as soon as it's added. Documentation siblings (top-level
`_comment_*` keys) and the CDK feature-flag keys (which start with `@`) are
filtered out, so they don't show up as false positives.

Special cases:

- `tags` is iterated by app.py and turned into literal AWS tags via
  `cdk.Tags.of(app).add(k, v)`. We assert it contains no documentation-style
  keys (anything starting with `_`) so a stray comment can never become a
  real tag.

- Pure-string-list blocks (e.g. `vpc_endpoint_cidrs`) and scalar
  context values (e.g. `project_name`) are skipped — there are no
  sub-keys to validate.

- Nested `_comment` siblings inside config blocks are skipped during the
  consumption check so per-section documentation doesn't fail the guard.

- CDK feature flags (top-level keys starting with `@aws-cdk` or `aws-cdk`)
  are interpreted by the CDK CLI itself, not the GCO source tree, so they
  are out of scope.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

PROJECT_ROOT = Path(__file__).parent.parent

# Blocks that are documented but intentionally not consumed by Python source.
# Empty for now — every active block is consumed. Add an entry here only when
# a block is read by something outside gco/ and lambda/ (e.g. CDK CLI, the
# Helm chart YAML directly), with a comment explaining where it's read.
EXEMPT_BLOCKS: set[str] = set()


def _load_cdk_context() -> dict[str, Any]:
    """Load the `context` section from cdk.json."""
    with open(PROJECT_ROOT / "cdk.json", encoding="utf-8") as f:
        data = json.load(f)
    context = data.get("context", {})
    assert isinstance(context, dict)
    return context


def _get_python_source() -> str:
    """Concatenate all Python source files in gco/ and lambda/ directories."""
    source_parts = []
    for directory in ("gco", "lambda"):
        source_dir = PROJECT_ROOT / directory
        for py_file in source_dir.rglob("*.py"):
            if "__pycache__" in str(py_file) or "-build" in str(py_file):
                continue
            source_parts.append(py_file.read_text(encoding="utf-8"))
    return "\n".join(source_parts)


def _is_documentation_key(key: str) -> bool:
    """Return True if `key` is a documentation sibling, not a real config key."""
    return key.startswith("_comment")


def _is_cdk_feature_flag(key: str) -> bool:
    """Return True if `key` is a CDK feature flag interpreted by the CDK CLI."""
    return key.startswith("@aws-cdk") or key.startswith("aws-cdk")


def _real_keys(block: dict[str, Any]) -> list[str]:
    """Return only the user-config keys in a block (drop documentation siblings)."""
    return [k for k in block if not _is_documentation_key(k)]


def _key_is_consumed(key: str, source: str) -> bool:
    """Return True if `key` appears in the source as a config-style reference."""
    patterns = (
        f'"{key}"',  # dict-literal key, double-quoted
        f"'{key}'",  # dict-literal key, single-quoted
        f".{key}",  # attribute access
        f'["{key}"]',  # bracket access, double-quoted
        f"['{key}']",  # bracket access, single-quoted
        f'get("{key}"',  # .get(...) call, double-quoted
        f"get('{key}'",  # .get(...) call, single-quoted
        # env-var convention used by inference_monitor and similar services
        key.upper(),
    )
    return any(p in source for p in patterns)


def _discover_dict_blocks(context: dict[str, Any]) -> list[tuple[str, dict[str, Any]]]:
    """Return (name, block) pairs for every dict block under context.

    Filters out documentation siblings, CDK feature flags, and the special
    `tags` block (which has its own dedicated test).
    """
    blocks: list[tuple[str, dict[str, Any]]] = []
    for name, value in context.items():
        if _is_documentation_key(name) or _is_cdk_feature_flag(name):
            continue
        if name == "tags":
            # Validated separately — every key becomes a real AWS tag.
            continue
        if name in EXEMPT_BLOCKS:
            continue
        if isinstance(value, dict):
            blocks.append((name, value))
    return blocks


# ─── Per-block consumption guard ─────────────────────────────────────────────

_CONTEXT = _load_cdk_context()
_BLOCKS = _discover_dict_blocks(_CONTEXT)


@pytest.mark.parametrize("block_name,block", _BLOCKS, ids=[name for name, _ in _BLOCKS])
def test_block_keys_are_consumed(block_name: str, block: dict[str, Any]) -> None:
    """Every key in this cdk.json block must be referenced somewhere in source."""
    source = _get_python_source()
    unconsumed = [key for key in _real_keys(block) if not _key_is_consumed(key, source)]
    if unconsumed:
        raise AssertionError(
            f"The following '{block_name}' config keys in cdk.json are not "
            f"referenced in any Python source file under gco/ or lambda/:\n"
            f"  {unconsumed}\n\n"
            f"Either consume them in the CDK stack or remove them from "
            f"cdk.json. If a key is consumed outside the Python source tree "
            f"(e.g. read directly by the CDK CLI or a Helm chart), add the "
            f"block name to EXEMPT_BLOCKS in this file with a comment "
            f"explaining where it's read."
        )


# ─── Tags block: documentation keys would leak into real AWS tags ─────────────


def test_tags_block_has_no_documentation_keys() -> None:
    """The `tags` block is iterated by app.py and emitted as literal AWS tags.

    Any documentation-style key inserted here (e.g. `_comment` or `_note`)
    would become a real AWS tag on every resource. Reject keys starting with
    `_` to keep `tags` clean.
    """
    tags = _CONTEXT.get("tags", {})
    leaked = [k for k in tags if k.startswith("_")]
    assert not leaked, (
        f"tags block must not contain documentation-style keys (any key "
        f"starting with '_'). Found: {leaked}. The `tags` dict is iterated "
        f"in app.py and every key/value pair becomes a real AWS tag — adding "
        f"a `_comment` here would tag every resource with the comment text."
    )


# ─── Coverage smoke check: make sure discovery actually finds blocks ─────────


def test_discovery_finds_expected_blocks() -> None:
    """Sanity check that auto-discovery picks up the major config blocks.

    If this fails, either cdk.json was restructured in a way that hid blocks
    from the consumption guard, or one of the listed blocks was renamed.
    """
    discovered = {name for name, _ in _BLOCKS}
    expected_subset = {
        "deployment_regions",
        "eks_cluster",
        "global_accelerator",
        "alb_config",
        "api_gateway",
        "waf",
        "manifest_processor",
        "queue_processor",
        "job_validation_policy",
        "resource_quota",
        "resource_thresholds",
        "images",
        "fsx_lustre",
        "s3_access_logs",
        "valkey",
        "aurora_pgvector",
        "analytics_environment",
        "helm",
        "inference_monitor",
        "mcp_server",
        "drift_detection",
    }
    missing = expected_subset - discovered
    assert not missing, (
        f"Auto-discovery is no longer finding these expected config blocks: "
        f"{sorted(missing)}. Was cdk.json restructured?"
    )
