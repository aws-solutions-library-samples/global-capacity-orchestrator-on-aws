"""The deploy command routes per-role autoscaling into the mooncake block.

The ``gco inference deploy`` command carries two independent autoscaling
surfaces. The legacy ``--min-replicas``/``--max-replicas``/``--autoscale-metric``
flags drive the single-Deployment autoscaler and land in the top-level
``spec.autoscaling`` block. The ``--mooncake-autoscale`` flag is the separate
per-role surface for split prefill/decode serving: each
``ROLE:MIN:MAX[:METRIC:TARGET]`` token sets the bounds (and optional metric)
for one role and is routed into ``spec.mooncake.autoscaling`` via the deploy
method's ``mooncake_autoscaling`` argument.

These checks drive the real click command with a mocked manager so nothing
touches AWS, and confirm the flag is parsed into the right per-role structure,
requires a split serving mode, and rejects malformed tokens.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from click.testing import CliRunner

from cli.main import cli


def _invoke(args: list[str]):
    """Run ``gco inference deploy`` with the manager mocked out.

    Returns ``(result, mock_manager)`` so a caller can assert on both the
    command exit and the keyword arguments handed to ``deploy``.
    """
    mock_manager = MagicMock()
    mock_manager.deploy.return_value = {
        "target_regions": ["us-east-1"],
        "ingress_path": "/inference/ep",
    }
    with patch("cli.inference.get_inference_manager", return_value=mock_manager):
        result = CliRunner().invoke(cli, ["inference", "deploy", *args])
    return result, mock_manager


def test_per_role_tokens_build_the_mooncake_autoscaling_block():
    """Two role tokens become a per-role ``mooncake.autoscaling`` mapping.

    The bounds land under each role and an optional ``METRIC:TARGET`` suffix
    becomes that role's metrics entry; the block is marked enabled.
    """
    result, manager = _invoke(
        [
            "ep",
            "-i",
            "img:v1",
            "--mooncake-mode",
            "disaggregated",
            "--mooncake-autoscale",
            "prefill:1:8",
            "--mooncake-autoscale",
            "decode:2:16:cpu:70",
        ]
    )

    assert result.exit_code == 0, result.output
    autoscaling = manager.deploy.call_args.kwargs["mooncake_autoscaling"]
    assert autoscaling == {
        "enabled": True,
        "prefill": {"min_replicas": 1, "max_replicas": 8},
        "decode": {
            "min_replicas": 2,
            "max_replicas": 16,
            "metrics": [{"type": "cpu", "target": 70}],
        },
    }


def test_legacy_autoscale_flag_is_kept_separate_from_the_mooncake_block():
    """``--autoscale-metric`` stays in the legacy block, not the mooncake one.

    A deploy that uses only the legacy flag passes no
    ``mooncake_autoscaling`` (it stays ``None``) while the legacy
    ``autoscaling`` argument is populated.
    """
    result, manager = _invoke(
        [
            "ep",
            "-i",
            "img:v1",
            "--mooncake-mode",
            "disaggregated",
            "--autoscale-metric",
            "cpu:70",
            "--min-replicas",
            "1",
            "--max-replicas",
            "4",
        ]
    )

    assert result.exit_code == 0, result.output
    kwargs = manager.deploy.call_args.kwargs
    assert kwargs["mooncake_autoscaling"] is None
    assert kwargs["autoscaling"]["enabled"] is True
    assert kwargs["autoscaling"]["min_replicas"] == 1
    assert kwargs["autoscaling"]["max_replicas"] == 4


def test_mooncake_autoscale_requires_a_serving_mode():
    """Using ``--mooncake-autoscale`` without ``--mooncake-mode`` is rejected.

    Without a mode the mooncake block is never built, so the flag would be
    silently dropped; the command errors out instead and persists nothing.
    """
    result, manager = _invoke(
        [
            "ep",
            "-i",
            "img:v1",
            "--mooncake-autoscale",
            "prefill:1:8",
        ]
    )

    assert result.exit_code == 1
    assert "requires --mooncake-mode" in result.output
    manager.deploy.assert_not_called()


def test_malformed_token_is_rejected():
    """A token without both bounds is rejected before any deploy call."""
    result, manager = _invoke(
        [
            "ep",
            "-i",
            "img:v1",
            "--mooncake-mode",
            "disaggregated",
            "--mooncake-autoscale",
            "prefill:1",
        ]
    )

    assert result.exit_code == 1
    assert "Invalid --mooncake-autoscale value" in result.output
    manager.deploy.assert_not_called()


def test_unknown_role_is_rejected():
    """A token naming a role other than prefill/decode is rejected."""
    result, manager = _invoke(
        [
            "ep",
            "-i",
            "img:v1",
            "--mooncake-mode",
            "disaggregated",
            "--mooncake-autoscale",
            "gpu:1:8",
        ]
    )

    assert result.exit_code == 1
    assert "Invalid --mooncake-autoscale role" in result.output
    manager.deploy.assert_not_called()


def test_non_integer_bound_is_rejected():
    """A non-integer bound is rejected with a clear message."""
    result, manager = _invoke(
        [
            "ep",
            "-i",
            "img:v1",
            "--mooncake-mode",
            "disaggregated",
            "--mooncake-autoscale",
            "prefill:one:8",
        ]
    )

    assert result.exit_code == 1
    assert "must be integers" in result.output
    manager.deploy.assert_not_called()
