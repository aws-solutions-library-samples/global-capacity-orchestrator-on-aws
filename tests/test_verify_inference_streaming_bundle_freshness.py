"""Tests for the bundle-freshness regression (``.github/scripts/verify_inference_streaming_bundle_freshness.py``).

In CI this script damages the real, git-ignored ``inference-streaming-proxy-build``
bundle and proves ``StackManager.synth`` / ``diff`` rebuild it with the pinned npm.
That end-to-end run needs the real bundle and a Node toolchain, which is why it
lives in ``unit:cdk:synth`` rather than here.

What *is* testable without npm is the script's own logic: how it locates the
dependency markers, how it picks a transitive dependency to delete, what
``_assert_fresh`` requires, and the shape of the damage-then-repair sequence in
``main``. Those run here against a synthetic project under ``tmp_path`` with one
seam faked -- ``StackManager._build_inference_streaming_proxy_lambda``, the npm
step -- replaced by a builder that produces the same end state through the
repository's *real* staging-and-publish machinery (``_prepare_lambda_asset``),
so every freshness manifest the script inspects is a genuine one.

The seam is asserted to still exist, so a rename in ``cli/stacks.py`` fails here
rather than turning this suite into a test of a stub.
"""

from __future__ import annotations

import importlib.util
import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

import cli.stacks as stacks

REPO_ROOT = Path(__file__).resolve().parent.parent
_SCRIPT = REPO_ROOT / ".github" / "scripts" / "verify_inference_streaming_bundle_freshness.py"

_spec = importlib.util.spec_from_file_location(
    "verify_inference_streaming_bundle_freshness", _SCRIPT
)
assert _spec is not None and _spec.loader is not None
freshness = importlib.util.module_from_spec(_spec)
sys.modules.setdefault("verify_inference_streaming_bundle_freshness", freshness)
_spec.loader.exec_module(freshness)

PACKAGE_JSON = {
    "name": "inference-streaming-proxy",
    "dependencies": {"@aws-sdk/client-bedrock-runtime": "3.0.0", "fast-xml-parser": "5.0.0"},
    "packageManager": "npm@12.0.2",
}


def _write_source(project_root: Path) -> Path:
    source = project_root / "lambda" / "inference-streaming-proxy"
    source.mkdir(parents=True)
    (source / "index.mjs").write_text(
        "export const handler = async () => 'ok';\n", encoding="utf-8"
    )
    (source / "package.json").write_text(json.dumps(PACKAGE_JSON), encoding="utf-8")
    (source / "package-lock.json").write_text('{"lockfileVersion": 3}\n', encoding="utf-8")
    return source


def _fake_npm_ci(source_dir: Path, staging_dir: Path) -> None:
    """Stand in for ``npm ci --omit=dev``: copy the package files, lay out node_modules.

    Produces one direct-dependency marker per declared dependency plus one
    transitive package (``tslib`` under the scoped SDK client), which is what the
    script's ``_transitive_dependency_file`` goes looking for.
    """
    for name in freshness._PACKAGE_FILES:
        shutil.copy2(source_dir / name, staging_dir / name)
    modules = staging_dir / "node_modules"
    for dependency in PACKAGE_JSON["dependencies"]:
        marker = modules / dependency / "package.json"
        marker.parent.mkdir(parents=True)
        marker.write_text(json.dumps({"name": dependency}), encoding="utf-8")
    transitive = modules / "tslib" / "package.json"
    transitive.parent.mkdir(parents=True)
    transitive.write_text(json.dumps({"name": "tslib"}), encoding="utf-8")


def _install_fake_builder(monkeypatch: pytest.MonkeyPatch) -> list[int]:
    """Replace the npm build step with the fake, routed through the real publisher."""
    builds: list[int] = []

    def fake_build(self: stacks.StackManager) -> None:
        source_dir, build_dir = stacks._INFERENCE_STREAMING_CDK_ASSET.paths(self.project_root)
        if not source_dir.is_dir():
            return
        published = stacks._prepare_lambda_asset(
            source_dir,
            build_dir,
            source_inputs=stacks._INFERENCE_STREAMING_CDK_ASSET.source_inputs,
            display_name="inference streaming Lambda (fake)",
            builder=lambda staging: _fake_npm_ci(source_dir, staging),
        )
        builds.append(1 if published else 0)

    assert hasattr(stacks.StackManager, "_build_inference_streaming_proxy_lambda"), (
        "the npm build seam this suite fakes has moved; update the fake and the CI script"
    )
    monkeypatch.setattr(stacks.StackManager, "_build_inference_streaming_proxy_lambda", fake_build)
    return builds


@pytest.fixture
def project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, list[int]]:
    """A synthetic project with a fresh, fake-built bundle, and the script pointed at it."""
    root = tmp_path / "project"
    _write_source(root)
    builds = _install_fake_builder(monkeypatch)
    manager = object.__new__(stacks.StackManager)
    manager.project_root = root
    manager._build_inference_streaming_proxy_lambda()  # initial bundle
    builds.clear()
    monkeypatch.setattr(freshness, "_PROJECT_ROOT", root)
    return root, builds


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def test_dependency_markers_name_one_package_json_per_direct_dependency(project) -> None:
    root, _ = project
    source = root / "lambda" / "inference-streaming-proxy"
    build = root / "lambda" / "inference-streaming-proxy-build"

    markers = freshness._dependency_markers(source, build)

    assert markers == (
        build / "node_modules" / "@aws-sdk/client-bedrock-runtime" / "package.json",
        build / "node_modules" / "fast-xml-parser" / "package.json",
    )


def test_dependency_markers_refuse_a_package_with_no_production_dependencies(
    tmp_path: Path,
) -> None:
    """A dependency-free package would make the marker check vacuous."""
    source = tmp_path / "src"
    source.mkdir()
    (source / "package.json").write_text('{"dependencies": {}}', encoding="utf-8")

    with pytest.raises(AssertionError, match="no production dependencies"):
        freshness._dependency_markers(source, tmp_path / "build")


def test_transitive_dependency_file_skips_direct_dependencies_and_scoped_prefixes(project) -> None:
    """The scoped SDK client is a *direct* dependency, so its marker must not be chosen."""
    root, _ = project
    source = root / "lambda" / "inference-streaming-proxy"
    build = root / "lambda" / "inference-streaming-proxy-build"

    marker = freshness._transitive_dependency_file(source, build)

    assert marker == build / "node_modules" / "tslib" / "package.json"


def test_transitive_dependency_file_refuses_a_flat_tree(tmp_path: Path) -> None:
    """Only direct dependencies present means nothing transitive can be deleted."""
    source = tmp_path / "src"
    source.mkdir()
    (source / "package.json").write_text(json.dumps(PACKAGE_JSON), encoding="utf-8")
    build = tmp_path / "build"
    for dependency in PACKAGE_JSON["dependencies"]:
        marker = build / "node_modules" / dependency / "package.json"
        marker.parent.mkdir(parents=True)
        marker.write_text("{}", encoding="utf-8")
    # A top-level file directly under node_modules has one path part and is skipped.
    (build / "node_modules" / "package.json").write_text("{}", encoding="utf-8")

    with pytest.raises(AssertionError, match="no transitive dependency marker"):
        freshness._transitive_dependency_file(source, build)


def test_assert_fresh_accepts_a_freshly_built_bundle(project) -> None:
    root, _ = project
    manager = object.__new__(stacks.StackManager)
    manager.project_root = root

    freshness._assert_fresh(
        manager,
        root / "lambda" / "inference-streaming-proxy",
        root / "lambda" / "inference-streaming-proxy-build",
    )


def test_assert_fresh_rejects_a_stale_manifest(project) -> None:
    root, _ = project
    build = root / "lambda" / "inference-streaming-proxy-build"
    (build / "index.mjs").write_bytes(b"// changed after the manifest was written\n")
    manager = object.__new__(stacks.StackManager)
    manager.project_root = root

    with pytest.raises(AssertionError, match="not source-current"):
        freshness._assert_fresh(manager, root / "lambda" / "inference-streaming-proxy", build)


def test_assert_fresh_rejects_a_bundle_that_is_fresh_but_missing_a_package_file(
    project, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The manifest can be lied to; the byte comparison cannot."""
    root, _ = project
    source = root / "lambda" / "inference-streaming-proxy"
    build = root / "lambda" / "inference-streaming-proxy-build"
    (build / "package-lock.json").write_text("tampered", encoding="utf-8")
    manager = object.__new__(stacks.StackManager)
    manager.project_root = root
    monkeypatch.setattr(manager, "_inference_streaming_build_is_fresh", lambda s, b: True)

    with pytest.raises(AssertionError, match="did not restore package-lock.json"):
        freshness._assert_fresh(manager, source, build)


def test_assert_fresh_rejects_a_bundle_missing_a_dependency_marker(
    project, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, _ = project
    source = root / "lambda" / "inference-streaming-proxy"
    build = root / "lambda" / "inference-streaming-proxy-build"
    shutil.rmtree(build / "node_modules" / "fast-xml-parser")
    manager = object.__new__(stacks.StackManager)
    manager.project_root = root
    monkeypatch.setattr(manager, "_inference_streaming_build_is_fresh", lambda s, b: True)

    with pytest.raises(AssertionError, match="did not restore dependency marker"):
        freshness._assert_fresh(manager, source, build)


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------


def test_main_damages_the_bundle_twice_and_the_builder_repairs_it_both_times(
    project, capsys: pytest.CaptureFixture[str]
) -> None:
    """The whole regression, with npm faked but the publish machinery real.

    Two damages -- rewritten handler, deleted transitive marker -- each detected
    as stale, each repaired by the production synth/diff entry points, with
    ``_run_cdk`` mocked so no CDK process runs.
    """
    root, builds = project

    freshness.main()

    assert builds == [1, 1], "expected exactly one rebuild per damage (synth, then diff)"
    build = root / "lambda" / "inference-streaming-proxy-build"
    assert (build / "node_modules" / "tslib" / "package.json").is_file()
    assert b"deliberate CI staleness" not in (build / "index.mjs").read_bytes()
    assert "freshness verified for synth and diff" in capsys.readouterr().out


def test_main_fails_if_the_handler_damage_is_not_detected(
    project, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A freshness check that accepts changed bytes would make the whole test vacuous."""
    monkeypatch.setattr(
        stacks.StackManager, "_inference_streaming_build_is_fresh", staticmethod(lambda s, b: True)
    )

    with pytest.raises(AssertionError, match="incorrectly accepted as fresh"):
        freshness.main()


def test_main_fails_if_synth_does_not_route_through_the_real_cdk_call(
    project, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The mocked ``_run_cdk`` must be called with the production argv, once."""

    def wrong_synth(self: stacks.StackManager, stack_name=None, quiet=True) -> str:  # noqa: ANN001
        self._ensure_lambda_build()
        return "something else"

    monkeypatch.setattr(stacks.StackManager, "synth", wrong_synth)

    with pytest.raises(AssertionError, match="Unexpected mocked synth result"):
        freshness.main()


def test_main_fails_if_a_deleted_transitive_marker_is_accepted_as_fresh(
    project, monkeypatch: pytest.MonkeyPatch
) -> None:
    """After the first repair the check must still see the second damage."""
    real = stacks.StackManager._inference_streaming_build_is_fresh
    calls = {"n": 0}

    def lenient_after_first_repair(source_dir: Path, build_dir: Path) -> bool:
        calls["n"] += 1
        # Call order in main(): initial (True), handler damage (False), post-synth
        # (True), marker damage -> pretend True here to trip the assertion.
        return True if calls["n"] == 4 else real(source_dir, build_dir)

    monkeypatch.setattr(
        stacks.StackManager,
        "_inference_streaming_build_is_fresh",
        staticmethod(lenient_after_first_repair),
    )

    with pytest.raises(AssertionError, match="Missing transitive dependency file was accepted"):
        freshness.main()


def test_main_fails_if_diff_does_not_route_through_the_real_cdk_call(
    project, monkeypatch: pytest.MonkeyPatch
) -> None:
    def wrong_diff(self: stacks.StackManager, stack_name=None) -> str:  # noqa: ANN001
        self._ensure_lambda_build()
        return "something else"

    monkeypatch.setattr(stacks.StackManager, "diff", wrong_diff)

    with pytest.raises(AssertionError, match="Unexpected mocked diff result"):
        freshness.main()


def test_main_fails_if_the_transitive_marker_is_not_restored(
    project, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A rebuild that leaves the tree incomplete must not pass on the manifest alone."""
    root, _ = project
    build = root / "lambda" / "inference-streaming-proxy-build"
    real_assert = freshness._assert_fresh
    seen = {"n": 0}

    def assert_fresh_then_delete_marker(manager, source_dir, build_dir) -> None:  # noqa: ANN001
        seen["n"] += 1
        real_assert(manager, source_dir, build_dir)
        if seen["n"] == 3:
            # After the diff repair, remove the marker again before main()'s own
            # final is_file() check runs.
            (build / "node_modules" / "tslib" / "package.json").unlink()

    monkeypatch.setattr(freshness, "_assert_fresh", assert_fresh_then_delete_marker)

    with pytest.raises(AssertionError, match="did not restore node_modules/tslib/package.json"):
        freshness.main()


# ---------------------------------------------------------------------------
# The real repository
# ---------------------------------------------------------------------------


def test_the_real_source_package_declares_production_dependencies() -> None:
    """The CI run's marker check is only meaningful if there are markers to check."""
    source = REPO_ROOT / "lambda" / "inference-streaming-proxy"
    build = REPO_ROOT / "lambda" / "inference-streaming-proxy-build"

    markers = freshness._dependency_markers(source, build)

    assert markers, "no direct dependencies to verify"
    assert all(
        marker.parent.parent.name == "node_modules" or "@" in str(marker) for marker in markers
    )


def test_the_ci_job_still_invokes_this_script() -> None:
    """If the workflow drops the step, this suite is testing a script nobody runs."""
    workflow = (REPO_ROOT / ".github" / "workflows" / "unit-tests.yml").read_text(encoding="utf-8")
    assert "verify_inference_streaming_bundle_freshness.py" in workflow


def test_the_script_is_directly_executable_without_pytest() -> None:
    """CI runs it as a file; the sys.path insert must make ``cli`` importable."""
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            f"import runpy; runpy.run_path({str(_SCRIPT)!r}, run_name='not_main')",
        ],
        capture_output=True,
        text=True,
        cwd=REPO_ROOT.parent,  # deliberately NOT the repo root
        check=False,
    )
    assert result.returncode == 0, result.stderr
