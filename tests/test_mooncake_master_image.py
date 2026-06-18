"""Default image wiring for the shared per-region Mooncake master.

The shared ``mooncake-master`` StatefulSet is materialized by the inference
monitor, which reads its image from the ``MOONCAKE_MASTER_IMAGE`` environment
variable (a per-endpoint ``spec.mooncake.store.master_image`` overrides it).
Without a default the master pod is created with an empty image and never
becomes Ready, so every disaggregated/store endpoint is stuck in 'creating'.

These tests pin the wiring that supplies that default end to end:

* the master default tracks the disaggregated role-pod default image (one tag
  to bump, never drifting apart),
* the inference-monitor manifest carries a ``MOONCAKE_MASTER_IMAGE`` env entry
  fed by the ``{{MOONCAKE_MASTER_IMAGE}}`` placeholder, and
* the regional stack populates that placeholder from the constant.

The inference-monitor manifest is a template whose unquoted ``{{...}}`` image
placeholders are not valid YAML until the regional stack substitutes them, so
these tests read it as text rather than parsing it.
"""

from __future__ import annotations

import re
from pathlib import Path

from cli.images import _DISAGGREGATED_DEFAULT_IMAGE
from gco.stacks.constants import MOONCAKE_MASTER_DEFAULT_IMAGE

MONITOR_MANIFEST_PATH = Path("lambda/kubectl-applier-simple/manifests/32-inference-monitor.yaml")
REGIONAL_STACK_PATH = Path("gco/stacks/regional_stack.py")
MASTER_IMAGE_PLACEHOLDER = "{{MOONCAKE_MASTER_IMAGE}}"


def test_master_default_tracks_disaggregated_role_image() -> None:
    """The master image default stays in lockstep with the role-pod default.

    Both run the same upstream vLLM image (it bundles the mooncake-transfer-
    engine, so the ``mooncake_master`` binary is on PATH). Keeping them equal
    means a single, pinned tag to validate and bump.
    """
    assert MOONCAKE_MASTER_DEFAULT_IMAGE == _DISAGGREGATED_DEFAULT_IMAGE


def test_master_default_is_pinned_not_rolling() -> None:
    """The default must be an explicit, reproducible tag — never ``latest``."""
    assert ":" in MOONCAKE_MASTER_DEFAULT_IMAGE
    tag = MOONCAKE_MASTER_DEFAULT_IMAGE.rsplit(":", 1)[1]
    assert tag and tag != "latest"


def test_monitor_manifest_sets_master_image_env() -> None:
    """The inference-monitor container declares MOONCAKE_MASTER_IMAGE from the
    placeholder the regional stack substitutes at deploy time.

    Verifies the env entry's ``name`` is immediately followed by a ``value``
    bound to the ``{{MOONCAKE_MASTER_IMAGE}}`` placeholder.
    """
    text = MONITOR_MANIFEST_PATH.read_text()
    entry = re.compile(
        r"-\s*name:\s*MOONCAKE_MASTER_IMAGE\s*\n\s*value:\s*\"\{\{MOONCAKE_MASTER_IMAGE\}\}\""
    )
    assert entry.search(text), (
        "inference-monitor manifest must declare a MOONCAKE_MASTER_IMAGE env entry "
        'whose value is "{{MOONCAKE_MASTER_IMAGE}}"'
    )


def test_regional_stack_populates_master_image_placeholder() -> None:
    """The regional stack maps the placeholder to the master default constant,
    so the deployed monitor receives a concrete image."""
    source = REGIONAL_STACK_PATH.read_text()
    assert MASTER_IMAGE_PLACEHOLDER in source, (
        "regional_stack.py must populate the {{MOONCAKE_MASTER_IMAGE}} replacement"
    )
    assert "MOONCAKE_MASTER_DEFAULT_IMAGE" in source, (
        "regional_stack.py must source the master image from the shared constant"
    )
