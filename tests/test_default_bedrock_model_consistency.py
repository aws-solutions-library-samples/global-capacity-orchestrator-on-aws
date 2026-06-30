"""The default Bedrock model id must stay identical across the two advisory
subsystems that resolve it independently.

``gco_mcp/mission/sampling.py`` (Mission strategy-revision / final-lessons
sampling) and ``cli/capacity/advisor.py`` (the Bedrock capacity advisor) each
pin their own default-model constant. They live in separate packages and are
kept in sync by intent rather than by a shared import: ``sampling`` stays
boto3-free at import time, while the advisor imports ``boto3`` at module load,
so neither can cleanly import the other's constant without dragging in an
unwanted dependency.

The monthly ``deps-scan`` workflow reads the *sampling* constant
(``DEFAULT_BEDROCK_MODEL_ID``) as the canonical pinned value when it checks for
a newer model release, so a silent divergence between the two constants would
make that drift report misleading — and would mean the CLI/advisor path and the
Mission path quietly use different models. This test turns that divergence into
a hard CI failure instead of a latent surprise.
"""

from __future__ import annotations

import sys
from pathlib import Path

# Mirror the path-injection pattern used across the Mission tests so
# ``mission.*`` resolves regardless of how pytest is invoked.
sys.path.insert(0, str(Path(__file__).parent.parent / "gco_mcp"))

from mission.sampling import DEFAULT_BEDROCK_MODEL_ID  # noqa: E402

from cli.capacity.advisor import BedrockCapacityAdvisor  # noqa: E402


def test_capacity_advisor_default_matches_mission_default() -> None:
    """The capacity advisor and Mission sampling must default to the same model.

    If you intend to diverge them, update this test deliberately — but the
    design (see the comment on ``DEFAULT_BEDROCK_MODEL_ID``) is that an operator
    gets the same model on both advisory paths out of the box.
    """
    assert BedrockCapacityAdvisor.DEFAULT_MODEL == DEFAULT_BEDROCK_MODEL_ID


def test_default_model_is_a_system_defined_inference_profile_id() -> None:
    """The default is a cross-Region (system-defined) inference profile id.

    Shape: ``<geo>.<provider>.<model-name>-v<MAJOR>:<MINOR>`` — the shape the
    deps-scan extractor and the family/version comparator in
    ``lib_dependency_scan.sh`` assume. Pinning the shape here keeps the scan and
    the source of truth from drifting apart structurally.
    """
    model_id = DEFAULT_BEDROCK_MODEL_ID
    assert model_id.startswith(("us.", "eu.", "apac.")), model_id
    # Trailing version coordinate, e.g. the "1:0" of "...-v1:0".
    assert "-v" in model_id, model_id
    assert ":" in model_id.rsplit("-v", 1)[-1], model_id
