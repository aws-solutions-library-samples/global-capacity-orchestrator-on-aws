"""
Tests for the GCO MCP server's feature-flag evaluation (gco_mcp/feature_flags.py).

Covers the truth-table strictness of `is_enabled` against arbitrary
environment-variable values: the function must return True if and only
if the case-insensitive, whitespace-stripped value equals the literal
"true". The property test under TestFeatureFlags exercises this against
hypothesis-generated input; example-based and umbrella-flag tests live
in sibling test classes.
"""

import os
import sys
from pathlib import Path
from unittest.mock import patch

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

# Ensure gco_mcp/ is importable
sys.path.insert(0, str(Path(__file__).parent.parent / "gco_mcp"))

import feature_flags  # noqa: E402


class TestFeatureFlags:
    """Tests for feature_flags.is_enabled and the umbrella flag."""

    @given(value=st.text().filter(lambda v: "\x00" not in v))
    @settings(max_examples=200)
    def test_is_enabled_truth_table_property(self, value: str) -> None:
        """is_enabled returns True iff the stripped, lowered value equals "true".

        The umbrella flag GCO_ENABLE_ALL_TOOLS is forced empty in the
        patched environment so the per-flag value is the only signal —
        otherwise an inherited GCO_ENABLE_ALL_TOOLS=true in the test
        runner's shell would mask the property under test. Null bytes
        are filtered out of the strategy because POSIX env-var values
        cannot contain them — this is a constraint of the input space
        being tested, not a property of is_enabled.
        """
        expected = value.strip().lower() == "true"
        with patch.dict(
            os.environ,
            {"GCO_TEST_FLAG": value, feature_flags.FLAG_ALL_TOOLS: ""},
        ):
            assert feature_flags.is_enabled("GCO_TEST_FLAG") is expected

    @pytest.mark.parametrize(
        ("value", "expected"),
        [
            ("true", True),
            ("True", True),
            ("TRUE", True),
            ("  true  ", True),
            ("True\n", True),
            ("", False),
            ("false", False),
            ("0", False),
            ("yes", False),
            ("1", False),
            (None, False),
        ],
        ids=[
            "literal-true",
            "title-case",
            "upper-case",
            "padded-whitespace",
            "trailing-newline",
            "empty-string",
            "literal-false",
            "zero",
            "yes",
            "one",
            "unset",
        ],
    )
    def test_is_enabled_truth_table(self, value: str | None, expected: bool) -> None:
        """is_enabled returns the expected verdict for each canonical input shape.

        Pins down the explicit cases the property test covers in aggregate:
        the four case/whitespace variants of "true" must all enable; empty,
        unset, and the common false-shaped values ("false", "0", "yes", "1")
        must all disable. The umbrella flag is forced empty so the per-flag
        value is the only signal.
        """
        env_overrides: dict[str, str] = {feature_flags.FLAG_ALL_TOOLS: ""}
        if value is not None:
            env_overrides["GCO_TEST_FLAG"] = value
            with patch.dict(os.environ, env_overrides):
                assert feature_flags.is_enabled("GCO_TEST_FLAG") is expected
        else:
            # Unset case: ensure GCO_TEST_FLAG is absent from the environment.
            with patch.dict(os.environ, env_overrides):
                os.environ.pop("GCO_TEST_FLAG", None)
                assert feature_flags.is_enabled("GCO_TEST_FLAG") is expected

    def test_all_tools_umbrella(self) -> None:
        """The All_Tools_Flag umbrella overrides every per-flag value.

        Three scenarios pin down the umbrella semantics:

        (a) Umbrella on + every per-flag unset: ``is_enabled`` returns
            ``True`` for every name in ``ALL_FLAGS`` — the umbrella
            alone is sufficient to enable every gated tool, no per-flag
            env var required.
        (b) Umbrella on + a per-flag explicitly set to ``"false"``:
            ``is_enabled`` still returns ``True``. Per the
            ``All_Tools_Flag`` definition the umbrella is mutually
            inclusive — explicit per-flag opt-outs do NOT shadow it.
        (c) Umbrella unset + one per-flag set to ``"true"``: only that
            named flag returns ``True``; the other ``ALL_FLAGS`` entries
            return ``False``. Confirms there is no implicit cross-flag
            leakage when the umbrella is off.

        ``FLAG_IMAGE_PUBLISH`` is exercised explicitly in addition to
        the iterating coverage in (a)–(c) so the named assertion stays
        in place even if ``ALL_FLAGS`` is later refactored.
        """
        # (a) Umbrella on, every per-flag unset → all enabled.
        env_a: dict[str, str] = {feature_flags.FLAG_ALL_TOOLS: "true"}
        with patch.dict(os.environ, env_a, clear=False):
            for flag in feature_flags.ALL_FLAGS:
                # Per-flag must be absent so we test umbrella in isolation.
                os.environ.pop(flag, None)
            for flag in feature_flags.ALL_FLAGS:
                assert feature_flags.is_enabled(flag) is True, (
                    f"umbrella on should enable {flag} (per-flag unset)"
                )
            # Explicit per-flag assertion for FLAG_IMAGE_PUBLISH (Req 1.11, 1.15).
            assert feature_flags.is_enabled(feature_flags.FLAG_IMAGE_PUBLISH) is True, (
                "umbrella on should enable FLAG_IMAGE_PUBLISH when its env var is unset"
            )

        # (b) Umbrella on, a per-flag explicitly "false" → still enabled.
        target = feature_flags.FLAG_DESTRUCTIVE_OPERATIONS
        env_b = {
            feature_flags.FLAG_ALL_TOOLS: "true",
            target: "false",
        }
        with patch.dict(os.environ, env_b, clear=False):
            assert feature_flags.is_enabled(target) is True, (
                f"umbrella must override explicit {target}=false"
            )

        # (b') Same shape, named explicitly for FLAG_IMAGE_PUBLISH.
        env_b_image = {
            feature_flags.FLAG_ALL_TOOLS: "true",
            feature_flags.FLAG_IMAGE_PUBLISH: "false",
        }
        with patch.dict(os.environ, env_b_image, clear=False):
            assert feature_flags.is_enabled(feature_flags.FLAG_IMAGE_PUBLISH) is True, (
                "umbrella must override explicit FLAG_IMAGE_PUBLISH=false"
            )

        # (c) Umbrella unset, a single per-flag on → only that flag enabled.
        env_c = {
            feature_flags.FLAG_ALL_TOOLS: "",
            target: "true",
        }
        with patch.dict(os.environ, env_c, clear=False):
            for flag in feature_flags.ALL_FLAGS:
                if flag == target:
                    continue
                # Other per-flags must be absent so the assertion below
                # tests "no umbrella + no per-flag set" cleanly.
                os.environ.pop(flag, None)
            assert feature_flags.is_enabled(target) is True
            for flag in feature_flags.ALL_FLAGS:
                if flag == target:
                    continue
                assert feature_flags.is_enabled(flag) is False, (
                    f"umbrella unset and {flag} unset → must be disabled"
                )
            # Explicit per-flag assertion: FLAG_IMAGE_PUBLISH is unset in this
            # block (popped above) and the umbrella is empty → must be False.
            assert feature_flags.is_enabled(feature_flags.FLAG_IMAGE_PUBLISH) is False, (
                "umbrella unset and FLAG_IMAGE_PUBLISH unset → must be disabled"
            )


class TestMissionFlag:
    """Tests for FLAG_MISSION (``GCO_ENABLE_MISSION``).

    Mirrors the per-flag verification pattern: each test isolates one
    leg of the truth table (umbrella off + per-flag off, umbrella off
    + per-flag on, umbrella on + per-flag explicitly off) so a regression
    in any single leg surfaces with a named, focused failure rather than
    a single sprawling test. The fourth test pins down the ``ALL_FLAGS``
    tuple membership so iterating callers (the umbrella test above, the
    feature-flag table emit in ``audit.emit_startup_log``) cover the new
    flag automatically.
    """

    def test_flag_mission_disabled_by_default(self) -> None:
        """FLAG_MISSION is disabled when both per-flag and umbrella are unset.

        This is the default posture for an MCP server that has not
        explicitly opted into Mission. The umbrella is forced empty
        and ``GCO_ENABLE_MISSION`` is popped so the assertion tests
        the unset case directly rather than an inherited shell value.
        """
        with patch.dict(os.environ, {feature_flags.FLAG_ALL_TOOLS: ""}, clear=False):
            os.environ.pop(feature_flags.FLAG_MISSION, None)
            assert feature_flags.is_enabled(feature_flags.FLAG_MISSION) is False

    def test_flag_mission_enabled_via_env(self) -> None:
        """FLAG_MISSION is enabled when GCO_ENABLE_MISSION="true" with umbrella unset.

        Confirms the per-flag opt-in path: setting only the named env
        var must enable the flag without requiring the umbrella. The
        umbrella is forced empty so this leg of the truth table is
        tested in isolation.
        """
        env = {
            feature_flags.FLAG_ALL_TOOLS: "",
            feature_flags.FLAG_MISSION: "true",
        }
        with patch.dict(os.environ, env, clear=False):
            assert feature_flags.is_enabled(feature_flags.FLAG_MISSION) is True

    def test_flag_mission_enabled_via_umbrella(self) -> None:
        """FLAG_MISSION is enabled by GCO_ENABLE_ALL_TOOLS=true even with per-flag=false.

        The umbrella is mutually inclusive — an explicit per-flag opt-out
        does NOT shadow it. This is the established semantic from
        ``feature_flags.is_enabled`` and Mission must not deviate from it.
        """
        env = {
            feature_flags.FLAG_ALL_TOOLS: "true",
            feature_flags.FLAG_MISSION: "false",
        }
        with patch.dict(os.environ, env, clear=False):
            assert feature_flags.is_enabled(feature_flags.FLAG_MISSION) is True

    def test_flag_mission_in_all_flags_tuple(self) -> None:
        """FLAG_MISSION is registered in the ALL_FLAGS tuple.

        The constant must be defined as ``GCO_ENABLE_MISSION`` and added
        to ``ALL_FLAGS`` so iterating callers (the umbrella test, the
        startup-log emitter) cover the new flag without further changes.
        """
        assert feature_flags.FLAG_MISSION == "GCO_ENABLE_MISSION"
        assert feature_flags.FLAG_MISSION in feature_flags.ALL_FLAGS
