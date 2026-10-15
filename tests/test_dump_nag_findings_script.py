"""Tests for ``scripts/dump_nag_findings.py``.

The script is a dev-only debugging helper that iterates the same
``NAG_CONFIGS`` list the pytest cdk-nag gate uses and prints every finding
grouped by ``(pack, rule, resource_path)``. These tests cover:

* ``run_config`` — the thin wrapper around
  ``tests.test_nag_compliance._build_app``,
  ``tests._cdk_app_builder.synthesize_app`` and
  ``tests.test_nag_compliance._collect_nag_violations``. We don't synth
  a real CDK app here (that's what ``test_nag_compliance`` does); we
  patch the helpers and assert ``run_config`` threads overrides through,
  synthesizes before reading the report, and returns the collected
  findings unchanged.
* ``main`` — argparse-less entry point. Asserts the output structure
  (``CONFIG:`` header per config, ``UNIQUE FINDINGS`` summary, exit
  code 0 on clean, 1 on any finding) against a stubbed ``run_config``
  and ``CONFIGS``.

The module loads the script via ``importlib`` because ``scripts/`` is
not a package — same pattern as ``test_analytics_lifecycle_script.py``.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

# ---------------------------------------------------------------------------
# Module-under-test import (same pattern as test_analytics_lifecycle_script)
# ---------------------------------------------------------------------------

_SCRIPT_PATH = Path(__file__).resolve().parent.parent / "scripts" / "dump_nag_findings.py"
_SPEC = importlib.util.spec_from_file_location("dump_nag_findings", _SCRIPT_PATH)
assert _SPEC is not None and _SPEC.loader is not None
dump_nag = importlib.util.module_from_spec(_SPEC)
sys.modules.setdefault("dump_nag_findings", dump_nag)
_SPEC.loader.exec_module(dump_nag)


# A finding dict has the shape produced by
# ``tests.test_nag_compliance._collect_nag_violations``:
#   {"pack": str, "rule": str, "description": str, "paths": list[str]}


# ---------------------------------------------------------------------------
# run_config
# ---------------------------------------------------------------------------


def test_run_config_returns_collected_findings_verbatim():
    """``run_config`` must return the collected violations without filtering."""
    expected = [
        {
            "pack": "AwsSolutions",
            "rule": "AwsSolutions-IAM5[Resource::*]",
            "description": "wildcard",
            "paths": ["/A/B"],
        },
        {
            "pack": "Serverless",
            "rule": "Serverless-LambdaTracing",
            "description": "tracing",
            "paths": ["/A/Lambda"],
        },
    ]
    app = MagicMock()

    with (
        patch.object(dump_nag, "_build_app", return_value=app) as mock_build,
        patch.object(dump_nag, "synthesize_app") as mock_synth,
        patch.object(dump_nag, "_collect_nag_violations", return_value=expected) as mock_collect,
    ):
        result = dump_nag.run_config("some-config", {"key": "value"})

    assert result == expected
    # overrides flow through to _build_app
    mock_build.assert_called_once_with(context_overrides={"key": "value"})
    mock_synth.assert_called_once_with(app)
    mock_collect.assert_called_once_with(app)


def test_run_config_empty_findings_list_when_clean():
    """A config that produces no findings returns an empty list, not None."""
    app = MagicMock()

    with (
        patch.object(dump_nag, "_build_app", return_value=app),
        patch.object(dump_nag, "synthesize_app"),
        patch.object(dump_nag, "_collect_nag_violations", return_value=[]),
    ):
        result = dump_nag.run_config("clean-config", {})

    assert result == []


def test_run_config_reads_findings_only_after_synthesizing():
    """The validation report is written by synthesis, so it is read afterwards.

    ``synthesize_app`` itself keeps the Docker image stand-in active for the
    whole synth (``tests/test_cdk_harness_contract.py`` pins that).
    """
    calls: list[str] = []
    app = MagicMock()

    def _record(name: str, result: object = None):
        def _call(*_args: object, **_kwargs: object) -> object:
            calls.append(name)
            return result

        return _call

    with (
        patch.object(dump_nag, "_build_app", side_effect=_record("build", app)),
        patch.object(dump_nag, "synthesize_app", side_effect=_record("synth")),
        patch.object(dump_nag, "_collect_nag_violations", side_effect=_record("collect", [])),
    ):
        dump_nag.run_config("c", {})

    assert calls == ["build", "synth", "collect"]


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------


def _fake_configs():
    """Shrink the live ``CONFIGS`` list to two entries so ``main`` finishes fast."""
    return [
        ("cfg-a", {"key": "a"}),
        ("cfg-b", {"key": "b"}),
    ]


def _finding(rule: str, path: str, pack: str = "AwsSolutions") -> dict[str, object]:
    return {"pack": pack, "rule": rule, "description": "", "paths": [path]}


def test_main_exits_zero_when_no_findings(capsys):
    """Main returns 0 iff every config produced an empty findings list."""
    with (
        patch.object(dump_nag, "CONFIGS", _fake_configs()),
        patch.object(dump_nag, "run_config", return_value=[]) as mock_run,
    ):
        rc = dump_nag.main()

    assert rc == 0
    # Called once per config
    assert mock_run.call_count == 2
    out = capsys.readouterr().out
    assert "CONFIG: cfg-a" in out
    assert "CONFIG: cfg-b" in out
    assert "UNIQUE FINDINGS ACROSS ALL CONFIGS: 0" in out


def test_main_exits_one_when_any_finding_present(capsys):
    """A single finding anywhere in the matrix must fail the gate."""
    finding = _finding("AwsSolutions-IAM5[Resource::*]", "/App/Stack/Role")
    with (
        patch.object(dump_nag, "CONFIGS", _fake_configs()),
        patch.object(dump_nag, "run_config", side_effect=[[], [finding]]),
    ):
        rc = dump_nag.main()

    assert rc == 1
    out = capsys.readouterr().out
    # Summary counts uniques, not occurrences.
    assert "UNIQUE FINDINGS ACROSS ALL CONFIGS: 1" in out
    assert "AwsSolutions-IAM5[Resource::*]" in out
    assert "/App/Stack/Role" in out
    # The summary should name the config the finding appeared in.
    assert "cfg-b" in out


def test_main_dedupes_findings_across_configs(capsys):
    """A finding that repeats across configs collapses to one entry.

    The summary should list both configs in the ``seen in:`` line rather
    than printing two separate entries.
    """
    repeat = _finding("AwsSolutions-IAM5[Resource::*]", "/App/Stack/Role")
    with (
        patch.object(dump_nag, "CONFIGS", _fake_configs()),
        patch.object(dump_nag, "run_config", side_effect=[[repeat], [repeat]]),
    ):
        rc = dump_nag.main()

    assert rc == 1
    out = capsys.readouterr().out
    assert "UNIQUE FINDINGS ACROSS ALL CONFIGS: 1" in out
    # Both config names appear on the same "seen in" line. ``sorted(set(...))``
    # produces alphabetical order.
    assert "cfg-a, cfg-b" in out


def test_main_uses_live_configs_list_when_not_patched():
    """Integration check: the real ``CONFIGS`` import resolves and is non-empty.

    We don't synthesize anything — we just confirm the list that ``main``
    would iterate is the gate's own ``NAG_CONFIGS``.
    """
    from tests._cdk_config_matrix import NAG_CONFIGS

    assert dump_nag.CONFIGS is NAG_CONFIGS
    assert len(dump_nag.CONFIGS) > 0
    # Each entry is a (name, overrides) tuple.
    for entry in dump_nag.CONFIGS:
        assert isinstance(entry, tuple)
        assert len(entry) == 2
        assert isinstance(entry[0], str)
        assert isinstance(entry[1], dict)


def test_main_prints_total_findings_per_config(capsys):
    """The per-config section header must include the count of findings."""
    findings_a = [
        _finding("R1[x]", "/A"),
        _finding("R2[y]", "/B"),
    ]
    with (
        patch.object(dump_nag, "CONFIGS", _fake_configs()),
        patch.object(dump_nag, "run_config", side_effect=[findings_a, []]),
    ):
        dump_nag.main()

    out = capsys.readouterr().out
    assert "total findings: 2" in out
    assert "total findings: 0" in out


# ---------------------------------------------------------------------------
# Script-level hygiene
# ---------------------------------------------------------------------------


def test_script_exports_expected_symbols():
    """``run_config`` and ``main`` are the public entry points."""
    assert callable(dump_nag.run_config)
    assert callable(dump_nag.main)


def test_script_puts_repo_root_on_sys_path():
    """The script adds the repo root to ``sys.path`` so ``tests.*`` imports
    resolve regardless of CWD.
    """
    repo_root = Path(__file__).resolve().parent.parent
    assert str(repo_root) in sys.path


def test_script_inserts_repo_root_when_it_is_missing_from_sys_path(monkeypatch):
    """Invoked from another CWD, the script prepends the repo root itself.

    The module-level guard is the only thing that makes ``tests.*`` importable
    when the script runs as ``python3 scripts/dump_nag_findings.py``. Re-execute
    the file with the root removed from ``sys.path`` and check it comes back at
    position 0 exactly once.
    """
    repo_root = str(Path(__file__).resolve().parent.parent)
    monkeypatch.setattr(sys, "path", [entry for entry in sys.path if entry != repo_root])
    assert repo_root not in sys.path

    spec = importlib.util.spec_from_file_location("dump_nag_findings_reexec", _SCRIPT_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    assert sys.path[0] == repo_root
    assert sys.path.count(repo_root) == 1
    assert str(module.REPO_ROOT) == repo_root
    # The re-executed copy binds the same shared matrix the pytest gate uses.
    assert module.CONFIGS is dump_nag.CONFIGS
