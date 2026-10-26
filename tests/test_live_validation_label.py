"""Contract for the deployed-path labeler (``.github/scripts/apply_live_validation_label.py``)
and the Dependabot split it pairs with.

The labeler adds ``live-validation-required`` when a pull request changes a
path that ships in a deployment; ``.github/dependabot.yml`` routes every
deployed graph into a monthly ``runtime`` group that carries the same label.
The tests hold three things:

1. **Path classification**: deployed directories and files are flagged,
   Markdown is never flagged, and CI, test and docs paths are left alone.
2. **Narrow blast radius**: the script only adds its one label and never
   removes anything.
3. **The Dependabot split**: each ecosystem sits whole in one group (the
   lockstep contracts in the file's header need that), deployed graphs are in
   ``runtime`` with the label on the entry so security updates carry it too,
   and the AWS CDK CLI is held out of the weekly batch.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from typing import Any

import pytest
import yaml

_PROJECT_ROOT = Path(__file__).resolve().parents[1]
_SCRIPT = _PROJECT_ROOT / ".github" / "scripts" / "apply_live_validation_label.py"
_DEPENDABOT = _PROJECT_ROOT / ".github" / "dependabot.yml"
_WORKFLOW = _PROJECT_ROOT / ".github" / "workflows" / "pr-type-label.yml"


def _load_script() -> Any:
    """Import the helper by path; ``.github`` is not an importable package."""
    spec = importlib.util.spec_from_file_location("apply_live_validation_label", _SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def script() -> Any:
    return _load_script()


@pytest.fixture(scope="module")
def dependabot() -> dict[str, Any]:
    loaded: dict[str, Any] = yaml.safe_load(_DEPENDABOT.read_text(encoding="utf-8"))
    return loaded


# ---------------------------------------------------------------------------
# Path classification
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "path",
    [
        "gco/stacks/regional_stack.py",
        "lambda/inference-streaming-proxy/package-lock.json",
        "lambda/helm-installer/Dockerfile",
        "dockerfiles/health-monitor-dockerfile",
        "app.py",
        "cdk.json",
        "pyproject.toml",
        "requirements-lock.txt",
        "package.json",
        "package-lock.json",
        "cli/stacks.py",
    ],
)
def test_deployed_paths_are_flagged(script: Any, path: str) -> None:
    assert script.deployed_paths([path]) == [path]


@pytest.mark.parametrize(
    "path",
    [
        ".github/workflows/security.yml",
        ".github/dependabot.yml",
        "tests/test_stacks.py",
        "docs/INFERENCE.md",
        "lambda/helm-installer/README.md",
        "gco/services/README.MD",
        "cli/commands/jobs_cmd.py",
        "gco_mcp/server.py",
        "lambda",
        "Gemfile.lock",
        "lambda/inference-streaming-proxy/CHANGELOG.md",
    ],
)
def test_other_paths_are_not_flagged(script: Any, path: str) -> None:
    assert script.deployed_paths([path]) == []


def test_matches_are_sorted_and_deduplicated(script: Any) -> None:
    paths = ["lambda/b.py", "docs/x.md", "app.py", "lambda/b.py", "gco/a.py"]
    assert script.deployed_paths(paths) == ["app.py", "gco/a.py", "lambda/b.py"]


# ---------------------------------------------------------------------------
# gh boundary
# ---------------------------------------------------------------------------


class _Completed:
    def __init__(self, stdout: str = "", returncode: int = 0, stderr: str = "") -> None:
        self.stdout = stdout
        self.returncode = returncode
        self.stderr = stderr


class _FakeGh:
    """Records ``gh`` calls and answers ``api`` / ``pr view`` / ``pr edit``."""

    def __init__(
        self,
        files: list[str],
        labels: list[str] | None = None,
        *,
        fail_edit: bool = False,
    ) -> None:
        self.files = files
        self.labels = labels
        self.fail_edit = fail_edit
        self.calls: list[list[str]] = []

    def __call__(self, argv: list[str], **_: Any) -> _Completed:
        assert argv[0] == "gh"
        args = argv[1:]
        self.calls.append(args)
        if args[0] == "api":
            return _Completed("".join(json.dumps(name) + "\n" for name in self.files) + "\n")
        if args[:2] == ["pr", "view"]:
            if self.labels is None:
                return _Completed(json.dumps({"labels": None}))
            return _Completed(json.dumps({"labels": [{"name": n} for n in self.labels]}))
        if args[:2] == ["pr", "edit"]:
            if self.fail_edit:
                return _Completed(returncode=1, stderr="HTTP 403: Resource not accessible")
            return _Completed()
        raise AssertionError(f"unexpected gh call: {args}")

    def edits(self) -> list[list[str]]:
        return [call for call in self.calls if call[:2] == ["pr", "edit"]]


def _install(monkeypatch: pytest.MonkeyPatch, script: Any, fake: _FakeGh) -> None:
    monkeypatch.setattr(script.subprocess, "run", fake)


def test_changed_files_pages_the_rest_endpoint_and_keeps_odd_names(
    script: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = _FakeGh(["gco/a b.py", "docs/x.md"])
    _install(monkeypatch, script, fake)
    assert script.changed_files(7) == ["gco/a b.py", "docs/x.md"]
    call = fake.calls[0]
    assert call[:2] == ["api", "--paginate"]
    assert "repos/{owner}/{repo}/pulls/7/files?per_page=100" in call


def test_current_labels_tolerates_a_null_label_list(
    script: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install(monkeypatch, script, _FakeGh([], labels=None))
    assert script.current_labels(7) == []


def test_a_failing_gh_command_surfaces_its_stderr(
    script: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install(monkeypatch, script, _FakeGh([], fail_edit=True))
    with pytest.raises(RuntimeError, match="HTTP 403"):
        script.add_label(7)


# ---------------------------------------------------------------------------
# main(): add-only, one label
# ---------------------------------------------------------------------------


def test_main_leaves_labels_alone_when_no_deployed_path_changed(
    script: Any, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    fake = _FakeGh([".github/workflows/lint.yml", "docs/CLI.md"], labels=[])
    _install(monkeypatch, script, fake)
    assert script.main(["--pr", "9"]) == 0
    assert fake.edits() == []
    assert "no deployed path changed" in capsys.readouterr().out


def test_main_adds_only_its_own_label(
    script: Any, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    fake = _FakeGh(["lambda/inference-streaming-proxy/package.json"], labels=["dependencies"])
    _install(monkeypatch, script, fake)
    assert script.main(["--pr", "9"]) == 0
    assert fake.edits() == [["pr", "edit", "9", "--add-label", script.LABEL]]
    out = capsys.readouterr().out
    assert "lambda/inference-streaming-proxy/package.json" in out
    assert "docs/LIVE_RELEASE_VALIDATION.md" in out


def test_main_is_a_no_op_when_the_label_is_already_applied(
    script: Any, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    fake = _FakeGh(["gco/a.py"], labels=[script.LABEL])
    _install(monkeypatch, script, fake)
    assert script.main(["--pr", "9"]) == 0
    assert fake.edits() == []
    assert "already applied" in capsys.readouterr().out


def test_main_dry_run_prints_the_plan_without_editing(
    script: Any, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    fake = _FakeGh(["cdk.json"], labels=[])
    _install(monkeypatch, script, fake)
    assert script.main(["--pr", "9", "--dry-run"]) == 0
    assert fake.edits() == []
    assert "dry run" in capsys.readouterr().out


def test_main_truncates_a_long_match_list(
    script: Any, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    fake = _FakeGh([f"gco/m{i:02d}.py" for i in range(25)], labels=[])
    _install(monkeypatch, script, fake)
    assert script.main(["--pr", "9"]) == 0
    out = capsys.readouterr().out
    assert "25 changed file(s)" in out
    assert "gco/m19.py" in out and "gco/m20.py" not in out
    assert "... and 5 more" in out


def test_never_removes_a_label(script: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _FakeGh(["docs/x.md"], labels=[script.LABEL, "dependencies"])
    _install(monkeypatch, script, fake)
    script.main(["--pr", "9"])
    assert not any("--remove-label" in call for call in fake.calls)


# ---------------------------------------------------------------------------
# The Dependabot split and the workflow wiring
# ---------------------------------------------------------------------------


def _entries(dependabot: dict[str, Any], group: str) -> list[dict[str, Any]]:
    return [u for u in dependabot["updates"] if u.get("multi-ecosystem-group") == group]


def _locations(entry: dict[str, Any]) -> list[str]:
    return list(entry.get("directories") or [entry["directory"]])


def test_dependabot_has_exactly_the_two_groups(dependabot: dict[str, Any]) -> None:
    groups = dependabot["multi-ecosystem-groups"]
    assert set(groups) == {"ci-tooling", "runtime"}
    assert groups["ci-tooling"]["schedule"]["interval"] == "weekly"
    assert groups["runtime"]["schedule"]["interval"] == "monthly"
    assert "live-validation-required" in groups["runtime"]["labels"]
    assert all(u.get("multi-ecosystem-group") in groups for u in dependabot["updates"])


def test_each_ecosystem_sits_whole_in_one_group(dependabot: dict[str, Any]) -> None:
    # Lockstep contracts: one SHA per action repository, one python tag across
    # every Dockerfile. Either split across groups could never pass CI.
    for ecosystem in ("github-actions", "docker"):
        groups = {
            u["multi-ecosystem-group"]
            for u in dependabot["updates"]
            if u["package-ecosystem"] == ecosystem
        }
        assert len(groups) == 1, f"{ecosystem} is split across {groups}"


def test_deployed_graphs_are_in_the_runtime_group_with_the_label(
    script: Any, dependabot: dict[str, Any]
) -> None:
    runtime = _entries(dependabot, "runtime")
    covered = {(u["package-ecosystem"], loc) for u in runtime for loc in _locations(u)}
    assert ("npm", "/lambda/inference-streaming-proxy") in covered
    for directory in ("/dockerfiles", "/lambda/helm-installer", "/lambda/tls-certificate-manager"):
        assert ("docker", directory) in covered
    for entry in runtime:
        # On the entry, not only the group: entry labels reach security updates.
        assert script.LABEL in entry["labels"], entry["package-ecosystem"]


def test_weekly_batch_holds_out_the_cdk_cli(dependabot: dict[str, Any]) -> None:
    (root_npm,) = [
        u
        for u in dependabot["updates"]
        if u["package-ecosystem"] == "npm" and u.get("directory") == "/"
    ]
    assert root_npm["multi-ecosystem-group"] == "ci-tooling"
    assert {"dependency-name": "aws-cdk"} in root_npm.get("ignore", [])


def test_workflow_runs_the_labeler_on_new_pushes_and_skips_dependabot() -> None:
    workflow = yaml.safe_load(_WORKFLOW.read_text(encoding="utf-8"))
    # PyYAML reads the bare `on:` key as boolean True.
    assert "synchronize" in workflow[True]["pull_request"]["types"]
    job = workflow["jobs"]["live-validation"]
    assert job["name"] == "pr:live-validation-label"
    assert "dependabot[bot]" in job["if"]
    assert job["permissions"] == {"contents": "read", "pull-requests": "write"}
    runs = [step.get("run", "") for step in job["steps"]]
    assert any("apply_live_validation_label.py" in run for run in runs)
