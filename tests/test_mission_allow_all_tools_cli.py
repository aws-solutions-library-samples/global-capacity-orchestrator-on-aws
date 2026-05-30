"""CLI tests for the ``--allow-all-tools`` flag on ``gco mission``.

Drives the ``start`` and ``run`` subcommands through Click's
``CliRunner`` against a ``FilesystemBackend`` rooted at ``tmp_path``,
mirroring the harness in ``test_mission_cli.py``: every test points the
module-level backend cache at the per-test temp directory so sessions
written by one test never leak into another, and the gating env var is
set per-test so the command group's gate does not block the subcommand
under test.

The all-tools branch reaches into the live MCP tool registry to resolve
the effective allowlist. To keep these tests fast and deterministic the
registry-resolution helper is patched to return a known, small registry
(or an empty one), so the resolved set is predictable and no real tool
registration happens during the test.

Cases:

* ``start --allow-all-tools`` with no ``--tool-allowlist`` succeeds and
  persists the resolved set; same for ``run`` (asserting the
  directive-only criteria fallback when the explicit list is empty).
* ``--allow-all-tools`` together with ``--tool-allowlist`` emits the
  mutual-exclusivity envelope, exits 1, and persists no session.
* ``--allow-all-tools`` against an empty registry emits
  ``allow_all_tools_empty_registry``, exits 1, and writes no session
  file or other state.
* The explicit-list path without the flag is unchanged.
"""

from __future__ import annotations

import importlib
import json
import sys
from pathlib import Path
from typing import Any

import pytest
from click.testing import CliRunner

# The Mission package lives under ``mcp/mission`` and is imported as
# ``mission.*``. Mirror the path-injection pattern used throughout the
# rest of the ``test_mission_*`` files so the imports below resolve
# regardless of how pytest is invoked.
sys.path.insert(0, str(Path(__file__).parent.parent / "mcp"))

from cli.main import cli  # noqa: E402

# The command-group package re-exports the ``mission`` Click group under
# the name ``mission_cmd``, which shadows the submodule attribute. Reach
# the real module object through importlib so the registry-resolution
# helper can be patched on it.
mission_cmd_mod = importlib.import_module("cli.commands.mission_cmd")

# ---------------------------------------------------------------------------
# Shared fixtures and helpers
# ---------------------------------------------------------------------------


@pytest.fixture
def isolated_backend(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """Point the Mission backend cache at a per-test temp directory.

    ``mission.state.get_backend()`` memoises the resolved backend in a
    module-level ``_BACKEND_INSTANCE`` so concurrent calls share state.
    Without overriding the cache every CLI test would write to
    ``~/.gco/missions/`` on the developer's machine.
    """
    from mission import state as mission_state  # noqa: PLC0415
    from mission.state import FilesystemBackend  # noqa: PLC0415

    backend = FilesystemBackend(root=tmp_path)
    monkeypatch.setattr(mission_state, "_BACKEND_INSTANCE", backend)
    yield tmp_path
    monkeypatch.setattr(mission_state, "_BACKEND_INSTANCE", None)


def _enable_flag(monkeypatch: pytest.MonkeyPatch) -> None:
    """Enable the Mission gate and clear the umbrella env var.

    The command group's gate accepts either flag; clearing the umbrella
    keeps the test focused on the per-tool flag and avoids cross-test
    interference from a developer machine that has the umbrella set.
    """
    monkeypatch.setenv("GCO_ENABLE_MISSION", "true")
    monkeypatch.delenv("GCO_ENABLE_ALL_TOOLS", raising=False)


def _patch_registry(
    monkeypatch: pytest.MonkeyPatch,
    registered: dict[str, Any],
    control: set[str],
) -> None:
    """Replace the CLI registry-resolution helper with a fixed snapshot.

    Returns ``(registered, control)`` verbatim so the all-tools branch
    resolves a predictable effective list without registering any real
    tools or walking the live FastMCP instance.
    """
    monkeypatch.setattr(
        mission_cmd_mod,
        "_resolve_registered_tools_for_cli",
        lambda: (registered, control),
    )


def _forbid_registry(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make the registry-resolution helper fail if it is ever called.

    The explicit-list path must never reach into the registry, so a
    test of that path patches the helper to raise: a clean run proves
    the registry was left untouched.
    """

    def _boom() -> Any:
        raise AssertionError("registry resolution should not run on the explicit path")

    monkeypatch.setattr(mission_cmd_mod, "_resolve_registered_tools_for_cli", _boom)


def _fake_registry() -> tuple[dict[str, Any], set[str]]:
    """A small registry with two ordinary tools and one control tool.

    The resolver reads only the dict keys, so the values can be any
    placeholder object. The control set names the one tool that must be
    filtered out of the all-tools expansion, leaving a stable resolved
    list of ``["find_examples", "list_jobs"]``.
    """
    registered: dict[str, Any] = {
        "list_jobs": object(),
        "find_examples": object(),
        "mission_start": object(),
    }
    control = {"mission_start"}
    return registered, control


_RESOLVED_SET = ["find_examples", "list_jobs"]


def _session_files(root: Path) -> list[Path]:
    """Return persisted session JSON files under ``root``.

    Excludes the ``*.report.json`` Final_Report sidecar so the count
    reflects only sessions that were actually saved.
    """
    return [p for p in root.glob("mission-*.json") if not p.name.endswith(".report.json")]


def _load_only_session(root: Path) -> dict[str, Any]:
    """Load the single persisted session under ``root`` as a dict."""
    files = _session_files(root)
    assert len(files) == 1, f"expected exactly one session file, found {files}"
    return json.loads(files[0].read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# start --allow-all-tools
# ---------------------------------------------------------------------------


class TestMissionStartAllowAllTools:
    """``gco mission start --allow-all-tools`` behaviour."""

    def test_start_allow_all_tools_without_explicit_list_persists_resolved_set(
        self,
        monkeypatch: pytest.MonkeyPatch,
        isolated_backend: Path,
    ) -> None:
        """``start --allow-all-tools`` with no explicit list saves the resolved set."""
        _enable_flag(monkeypatch)
        _patch_registry(monkeypatch, *_fake_registry())

        runner = CliRunner()
        result = runner.invoke(
            cli,
            [
                "mission",
                "start",
                "--directive",
                "Stabilize the widget pipeline configuration.",
                "--max-iterations",
                "5",
                "--max-wall-clock",
                "60",
                "--allow-all-tools",
                "--with-defaults",
            ],
        )

        assert result.exit_code == 0, f"stderr: {result.stderr}\nstdout: {result.stdout}"
        payload = json.loads(result.stdout)
        assert payload["status"] == "pending"

        session = _load_only_session(isolated_backend)
        # The resolved allowlist is the registry minus the control tool,
        # sorted for a stable persisted order.
        assert session["tool_allowlist"] == _RESOLVED_SET

    def test_start_allow_all_tools_with_explicit_list_is_rejected(
        self,
        monkeypatch: pytest.MonkeyPatch,
        isolated_backend: Path,
    ) -> None:
        """Combining the flag with an explicit list rejects and saves nothing."""
        _enable_flag(monkeypatch)
        _patch_registry(monkeypatch, *_fake_registry())

        runner = CliRunner()
        result = runner.invoke(
            cli,
            [
                "mission",
                "start",
                "--directive",
                "Stabilize the widget pipeline configuration.",
                "--max-iterations",
                "5",
                "--max-wall-clock",
                "60",
                "--allow-all-tools",
                "--tool-allowlist",
                "find_examples",
                "--with-defaults",
            ],
        )

        assert result.exit_code == 1
        envelope = json.loads(result.stderr)
        assert envelope["code"] == "validation_error"
        assert envelope["details"]["field"] == "tool_allowlist"
        assert (
            envelope["details"]["reason"] == "allow_all_and_explicit_allowlist_mutually_exclusive"
        )
        # No session was persisted before the rejection.
        assert _session_files(isolated_backend) == []

    def test_start_allow_all_tools_empty_registry_is_rejected(
        self,
        monkeypatch: pytest.MonkeyPatch,
        isolated_backend: Path,
    ) -> None:
        """An empty registry rejects with no session or other state written."""
        _enable_flag(monkeypatch)
        _patch_registry(monkeypatch, {}, set())

        runner = CliRunner()
        result = runner.invoke(
            cli,
            [
                "mission",
                "start",
                "--directive",
                "Stabilize the widget pipeline configuration.",
                "--max-iterations",
                "5",
                "--max-wall-clock",
                "60",
                "--allow-all-tools",
                "--with-defaults",
            ],
        )

        assert result.exit_code == 1
        envelope = json.loads(result.stderr)
        assert envelope["code"] == "validation_error"
        assert envelope["details"]["field"] == "tool_allowlist"
        assert envelope["details"]["reason"] == "allow_all_tools_empty_registry"
        # No session file and no report sidecar exist under the root.
        assert list(isolated_backend.glob("mission-*.json")) == []


# ---------------------------------------------------------------------------
# run --allow-all-tools
# ---------------------------------------------------------------------------


class TestMissionRunAllowAllTools:
    """``gco mission run --allow-all-tools`` behaviour."""

    def test_run_allow_all_tools_without_explicit_list_persists_resolved_set(
        self,
        monkeypatch: pytest.MonkeyPatch,
        isolated_backend: Path,
    ) -> None:
        """``run --allow-all-tools`` saves the resolved set and scaffolds from the directive only.

        With no explicit list the criteria scaffolder receives an empty
        allowlist and falls back to its directive-only deterministic
        path (a single placeholder predicate rather than any per-tool
        criterion). Resolution then fills the persisted allowlist from
        the patched registry. ``--no-sampling`` keeps the scaffolder on
        the deterministic path and ``--dry-run`` keeps the loop off the
        live tool dispatcher.
        """
        _enable_flag(monkeypatch)
        _patch_registry(monkeypatch, *_fake_registry())

        runner = CliRunner()
        result = runner.invoke(
            cli,
            [
                "mission",
                "run",
                "--directive",
                "Stabilize the widget pipeline configuration.",
                "--max-iterations",
                "1",
                "--max-wall-clock",
                "60",
                "--allow-all-tools",
                "--no-sampling",
                "--dry-run",
            ],
        )

        assert result.exit_code == 0, f"stderr: {result.stderr}\nstdout: {result.stdout}"

        session = _load_only_session(isolated_backend)
        assert session["tool_allowlist"] == _RESOLVED_SET

        # The directive matched no keyword template, so the empty-allowlist
        # scaffolder produced a single directive-only placeholder predicate
        # and no per-tool ``tool_call_succeeded`` criteria.
        criteria = session["criteria"]
        assert len(criteria) == 1
        assert criteria[0]["kind"] == "predicate"
        assert all(c["kind"] != "tool_call_succeeded" for c in criteria)

    def test_run_allow_all_tools_with_explicit_list_is_rejected(
        self,
        monkeypatch: pytest.MonkeyPatch,
        isolated_backend: Path,
    ) -> None:
        """Combining the flag with an explicit list rejects before any side effect."""
        _enable_flag(monkeypatch)
        _patch_registry(monkeypatch, *_fake_registry())

        runner = CliRunner()
        result = runner.invoke(
            cli,
            [
                "mission",
                "run",
                "--directive",
                "Stabilize the widget pipeline configuration.",
                "--max-iterations",
                "1",
                "--allow-all-tools",
                "--tool-allowlist",
                "find_examples",
                "--no-sampling",
                "--dry-run",
            ],
        )

        assert result.exit_code == 1
        envelope = json.loads(result.stderr)
        assert envelope["code"] == "validation_error"
        assert (
            envelope["details"]["reason"] == "allow_all_and_explicit_allowlist_mutually_exclusive"
        )
        # The rejection happens up front: no session and no report sidecar.
        assert list(isolated_backend.glob("mission-*.json")) == []

    def test_run_allow_all_tools_empty_registry_is_rejected(
        self,
        monkeypatch: pytest.MonkeyPatch,
        isolated_backend: Path,
    ) -> None:
        """An empty registry rejects up front with no state written."""
        _enable_flag(monkeypatch)
        _patch_registry(monkeypatch, {}, set())

        runner = CliRunner()
        result = runner.invoke(
            cli,
            [
                "mission",
                "run",
                "--directive",
                "Stabilize the widget pipeline configuration.",
                "--max-iterations",
                "1",
                "--allow-all-tools",
                "--no-sampling",
                "--dry-run",
            ],
        )

        assert result.exit_code == 1
        envelope = json.loads(result.stderr)
        assert envelope["code"] == "validation_error"
        assert envelope["details"]["reason"] == "allow_all_tools_empty_registry"
        # Resolution runs before scaffolding and persistence, so nothing
        # at all is written under the root.
        assert list(isolated_backend.glob("mission-*.json")) == []


# ---------------------------------------------------------------------------
# Explicit-list path without the flag — unchanged behaviour
# ---------------------------------------------------------------------------


class TestMissionExplicitListUnchanged:
    """The explicit-list path without ``--allow-all-tools`` is unaffected."""

    def test_start_explicit_list_without_flag_persists_and_skips_registry(
        self,
        monkeypatch: pytest.MonkeyPatch,
        isolated_backend: Path,
    ) -> None:
        """An explicit list without the flag persists verbatim and never touches the registry."""
        _enable_flag(monkeypatch)
        # The explicit path must not reach into the registry at all.
        _forbid_registry(monkeypatch)

        runner = CliRunner()
        result = runner.invoke(
            cli,
            [
                "mission",
                "start",
                "--directive",
                "Stabilize the widget pipeline configuration.",
                "--max-iterations",
                "5",
                "--max-wall-clock",
                "60",
                "--tool-allowlist",
                "find_examples",
                "--with-defaults",
            ],
        )

        assert result.exit_code == 0, f"stderr: {result.stderr}\nstdout: {result.stdout}"
        session = _load_only_session(isolated_backend)
        assert session["tool_allowlist"] == ["find_examples"]

    def test_start_without_flag_and_without_list_is_rejected_empty(
        self,
        monkeypatch: pytest.MonkeyPatch,
        isolated_backend: Path,
    ) -> None:
        """No flag and no explicit list rejects with the existing empty reason."""
        _enable_flag(monkeypatch)
        _forbid_registry(monkeypatch)

        runner = CliRunner()
        result = runner.invoke(
            cli,
            [
                "mission",
                "start",
                "--directive",
                "Stabilize the widget pipeline configuration.",
                "--max-iterations",
                "5",
                "--max-wall-clock",
                "60",
                "--with-defaults",
            ],
        )

        assert result.exit_code == 1
        envelope = json.loads(result.stderr)
        assert envelope["code"] == "validation_error"
        assert envelope["details"]["field"] == "tool_allowlist"
        assert envelope["details"]["reason"] == "empty"
        assert _session_files(isolated_backend) == []

    def test_run_explicit_list_without_flag_persists_and_skips_registry(
        self,
        monkeypatch: pytest.MonkeyPatch,
        isolated_backend: Path,
    ) -> None:
        """``run`` with an explicit list and no flag persists it and skips the registry."""
        _enable_flag(monkeypatch)
        _forbid_registry(monkeypatch)

        runner = CliRunner()
        result = runner.invoke(
            cli,
            [
                "mission",
                "run",
                "--directive",
                "Stabilize the widget pipeline configuration.",
                "--max-iterations",
                "1",
                "--max-wall-clock",
                "60",
                "--tool-allowlist",
                "find_examples",
                "--no-sampling",
                "--dry-run",
            ],
        )

        assert result.exit_code == 0, f"stderr: {result.stderr}\nstdout: {result.stdout}"
        session = _load_only_session(isolated_backend)
        assert session["tool_allowlist"] == ["find_examples"]
