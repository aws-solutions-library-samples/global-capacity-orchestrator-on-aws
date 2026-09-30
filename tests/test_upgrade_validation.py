"""Offline coverage for the upgrade-validation harness (``scripts/upgrade_validation``).

The harness deploys the previous release with that release's own ``gco``,
upgrades it to the checked-out commit with ``gco upgrade``, verifies the
result, and tears it down. Nothing here touches AWS. The workspace mechanics
(the private mirror that shares objects with the operator's repository, the
clone at the base tag, the synthetic release tag, the run-scoped cdk.json) run
against a real temporary git repository; commands run as real subprocesses;
AWS clients, the reused release-harness actions, and the adoption primitive
are faked. The registry is held in lockstep with the runbook's contract table
and the developer README, like the sibling harnesses.
"""

from __future__ import annotations

import json
import re
import signal
import subprocess
import sys
import threading
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pytest
import requests

from cli.aws_client import APIRequestError
from scripts.live_release_validation.models import ActionFailure, RunSettings
from scripts.upgrade_validation import actions
from scripts.upgrade_validation import sentinel as sentinel_module
from scripts.upgrade_validation import workspace as workspace_module
from scripts.upgrade_validation.models import PROVIDER_LOG_CONTEXT, UpgradeRunSettings
from scripts.upgrade_validation.registry import build_action_registry
from scripts.upgrade_validation.workspace import CommandResult, Workspace

REPO_ROOT = Path(__file__).resolve().parent.parent
PACKAGE = REPO_ROOT / "scripts" / "upgrade_validation"
RUNBOOK = REPO_ROOT / "docs" / "UPGRADE_VALIDATION.md"
DEVELOPER_README = PACKAGE / "README.md"

_ACCOUNT = "123456789012"
_RUN_ID = "run-1"
_PROJECT = "gco-live"
_REGION = "us-east-1"
_REGIONS = {
    "global": "us-east-2",
    "api_gateway": "us-east-2",
    "monitoring": "us-east-2",
    "regional": [_REGION],
}
_CONTROL = ["gco-live-global", "gco-live-api-gateway", "gco-live-monitoring"]
_WORKLOAD = ["gco-live-regional-api-us-east-1", "gco-live-us-east-1"]
_TARGETS = dict.fromkeys(_CONTROL, "us-east-2") | dict.fromkeys(_WORKLOAD, _REGION)
_DATE_HEADER = "Tue, 29 Sep 2026 12:00:00 GMT"


# ─── A real repository with a release and a candidate ───────────────


def _git(cwd: Path, *arguments: str) -> str:
    result = subprocess.run(
        [
            "git",
            "-c",
            "user.name=Harness Test",
            "-c",
            "user.email=harness@example.com",
            "-c",
            "commit.gpgsign=false",
            "-c",
            "tag.gpgsign=false",
            "-c",
            "core.hooksPath=/dev/null",
            *arguments,
        ],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


@dataclass(frozen=True)
class Repo:
    root: Path
    head: str
    base: str


def _cdk_json(**context: Any) -> str:
    document = {
        "app": "python3 app.py",
        "context": {
            "project_name": _PROJECT,
            "deployment_regions": _REGIONS,
            "tags": {"Project": "GCO"},
            **context,
        },
    }
    return json.dumps(document, indent=2) + "\n"


@pytest.fixture
def repo(tmp_path: Path) -> Repo:
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "--quiet", "-b", "main")
    (root / "cdk.json").write_text(_cdk_json(), encoding="utf-8")
    (root / "requirements-lock.txt").write_text("boto3==1.40.0\n", encoding="utf-8")
    (root / "VERSION").write_text("1.0.0\n", encoding="utf-8")
    _git(root, "add", "-A")
    _git(root, "commit", "--quiet", "-m", "release 1.0.0")
    _git(root, "tag", "-a", "v1.0.0", "-m", "v1.0.0")
    _git(root, "checkout", "--quiet", "-b", "feature")
    (root / "VERSION").write_text("1.0.1-dev\n", encoding="utf-8")
    _git(root, "commit", "--quiet", "-am", "candidate")
    return Repo(root, _git(root, "rev-parse", "HEAD"), _git(root, "rev-parse", "v1.0.0^{commit}"))


def _settings(tmp_path: Path, repo: Repo | None = None, **overrides: Any) -> UpgradeRunSettings:
    report = tmp_path / "reports" / _RUN_ID
    root = repo.root if repo is not None else tmp_path / "repo"
    root.mkdir(exist_ok=True)
    values: dict[str, Any] = {
        "run_id": _RUN_ID,
        "repo_root": root,
        "report_dir": report,
        "checkpoint_path": report / "checkpoint.json",
        "expected_account": _ACCOUNT,
        "expected_sha": repo.head if repo is not None else "a" * 40,
        "expected_branch": "feature",
        "profile": "configured",
        "requested_actions": ("all",),
        "base_ref": "v1.0.0",
        "base_commit": repo.base if repo is not None else "b" * 40,
        "workspace_dir": report.with_name(f"{_RUN_ID}.workspace"),
        "min_free_disk_gib": 0,
    }
    values.update(overrides)
    return UpgradeRunSettings(**values)


def _sts_client(account: str = _ACCOUNT, date: str | None = _DATE_HEADER) -> MagicMock:
    sts = MagicMock(name="sts")
    headers = {"date": date} if date is not None else {}
    sts.get_caller_identity.return_value = {
        "Account": account,
        "ResponseMetadata": {"HTTPHeaders": headers},
    }
    return sts


def _ctx(
    settings: UpgradeRunSettings,
    *,
    state: dict[str, Any] | None = None,
    deployment_attempted: bool = False,
    selected: tuple[str, ...] = tuple(build_action_registry()),
) -> Any:
    context = SimpleNamespace(
        settings=settings,
        checkpoint=SimpleNamespace(
            state=state if state is not None else {"target_stack_regions": dict(_TARGETS)},
            baseline={"ecr_repositories": {}},
            deployment_attempted=deployment_attempted,
            destroyed=False,
        ),
        report=SimpleNamespace(selected_actions=list(selected)),
        cdk_context={"project_name": _PROJECT, "deployment_regions": _REGIONS},
        config=SimpleNamespace(
            project_name=_PROJECT,
            global_region="us-east-2",
            api_gateway_region="us-east-2",
            monitoring_region="us-east-2",
            default_region=_REGION,
        ),
        session=MagicMock(name="session"),
        aws_client=MagicMock(name="aws_client"),
        state_lock=threading.RLock(),
        persist=MagicMock(name="persist"),
        persist_callback=MagicMock(name="persist_callback"),
    )
    context.session.client.return_value = _sts_client()
    return context


def _result(
    exit_code: int = 0,
    *,
    stdout: str = "",
    timed_out: bool = False,
    log: Path = Path("/tmp/step.log"),
) -> CommandResult:
    return CommandResult(
        argv=("gco",),
        exit_code=exit_code,
        duration_seconds=1.0,
        timed_out=timed_out,
        log_path=log,
        stdout=stdout,
        tail=("last line",),
    )


# ─── Settings ────────────────────────────────────────────────────────


class TestUpgradeRunSettings:
    def test_identity_pins_the_base_release_and_the_ownership_model(self, tmp_path: Path) -> None:
        settings = _settings(tmp_path)
        identity = settings.identity()
        assert identity["base_ref"] == "v1.0.0"
        assert identity["base_commit"] == "b" * 40
        assert identity["workspace_dir"] == str(tmp_path / "reports" / f"{_RUN_ID}.workspace")
        assert identity["stack_ownership"] == "run-tag-adoption"
        assert identity["base_cdk_context"] == {
            "gco_live_validation_disable_efs_automatic_backups": "true",
            PROVIDER_LOG_CONTEXT: "true",
        }
        assert identity["expected_sha"] == "a" * 40
        assert UpgradeRunSettings.allows_run_tag_adoption is True
        assert RunSettings.allows_run_tag_adoption is False

    def test_the_workspace_path_is_normalized(self, tmp_path: Path) -> None:
        settings = _settings(tmp_path, workspace_dir=tmp_path / "x" / ".." / "ws")
        assert settings.workspace_dir == tmp_path / "ws"

    @pytest.mark.parametrize(
        ("overrides", "match"),
        [
            ({"base_ref": "8.8.0"}, "must be a release tag"),
            ({"base_ref": "v8.8"}, "must be a release tag"),
            ({"base_commit": "B" * 40}, "lowercase 40-character"),
            ({"base_commit": "b" * 12}, "lowercase 40-character"),
            ({"base_commit": "a" * 40}, "nothing to upgrade"),
            ({"deploy_timeout_seconds": 0}, "deploy_timeout_seconds must be positive"),
            ({"upgrade_timeout_seconds": -1}, "upgrade_timeout_seconds must be positive"),
            ({"api_ready_timeout_seconds": 0}, "api_ready_timeout_seconds must be positive"),
        ],
    )
    def test_invalid_inputs_are_refused(
        self, tmp_path: Path, overrides: dict[str, Any], match: str
    ) -> None:
        with pytest.raises(ValueError, match=match):
            _settings(tmp_path, **overrides)

    @pytest.mark.parametrize(
        ("workspace", "match"),
        [
            ("repo/ws", "overlap the checkout"),
            ("repo", "overlap the checkout"),
            (".", "overlap the checkout"),
            ("reports/run-1", "overlap the report directory"),
            ("reports/run-1/ws", "overlap the report directory"),
            ("reports", "overlap the report directory"),
        ],
    )
    def test_the_workspace_must_not_overlap_the_checkout_or_reports(
        self, tmp_path: Path, workspace: str, match: str
    ) -> None:
        with pytest.raises(ValueError, match=match):
            _settings(tmp_path, workspace_dir=tmp_path / workspace)


# ─── Registry, runbook, and README ───────────────────────────────────

_RUNBOOK_ROW = re.compile(r"^\|\s*`([a-z-]+)`\s*\|([^|]*)\|")


class TestActionRegistry:
    def test_order_and_dependencies(self) -> None:
        registry = build_action_registry()
        assert list(registry) == [
            "preflight",
            "baseline",
            "prepare",
            "deploy",
            "sentinels",
            "upgrade",
            "topology",
            "verify-upgrade",
            "destroy",
            "final-inventory",
        ]
        assert registry["deploy"].dependencies == ("baseline", "prepare")
        assert registry["verify-upgrade"].dependencies == ("topology",)
        for index, definition in enumerate(registry.values()):
            assert set(definition.dependencies) <= set(list(registry)[:index]), definition.name
            assert definition.handler.__doc__, definition.name

    def test_the_runner_derives_the_deploy_dependents(self) -> None:
        from scripts.live_release_validation.runner import LiveValidationRunner

        derived = LiveValidationRunner._derive_deploy_dependent_actions(build_action_registry())
        assert derived == frozenset(
            {"deploy", "sentinels", "upgrade", "topology", "verify-upgrade"}
        )

    def test_reused_actions_are_the_release_harness_handlers(self) -> None:
        from scripts.live_release_validation import actions as release_actions

        registry = build_action_registry()
        assert registry["baseline"].handler is release_actions.action_baseline
        assert registry["topology"].handler is release_actions.action_topology
        assert registry["destroy"].handler is release_actions.action_destroy
        assert registry["final-inventory"].handler is release_actions.action_final_inventory

    def test_the_runbook_contract_table_matches_the_registry(self) -> None:
        rows = []
        for line in RUNBOOK.read_text(encoding="utf-8").splitlines():
            match = _RUNBOOK_ROW.match(line.strip())
            if match:
                rows.append((match.group(1), match.group(2).strip()))
        registry = build_action_registry()
        assert [name for name, _cell in rows] == list(registry)
        for name, cell in rows:
            expected = ", ".join(f"`{item}`" for item in registry[name].dependencies) or "None"
            assert cell == expected, name

    def test_the_developer_readme_names_every_action_and_module(self) -> None:
        readme = DEVELOPER_README.read_text(encoding="utf-8")
        for name in build_action_registry():
            assert f"| `{name}` |" in readme, name
        for module in sorted(PACKAGE.glob("*.py")):
            if module.name != "__init__.py":
                assert f"`{module.name}`" in readme, module.name

    def test_the_docs_index_and_cross_references_exist(self) -> None:
        index = (REPO_ROOT / "docs" / "README.md").read_text(encoding="utf-8")
        assert "[Upgrade Validation](UPGRADE_VALIDATION.md)" in index
        for doc in ("UPGRADING.md", "LIVE_RELEASE_VALIDATION.md", "CLI.md"):
            assert "UPGRADE_VALIDATION.md" in (REPO_ROOT / "docs" / doc).read_text(
                encoding="utf-8"
            ), doc
        assert "#### `gco release validate-upgrade`" in (REPO_ROOT / "docs" / "CLI.md").read_text(
            encoding="utf-8"
        )

    def test_the_report_files_are_allowed_in_a_private_run_directory(self) -> None:
        from scripts.live_release_validation.artifact_io import REPORT_FILENAMES
        from scripts.upgrade_validation.__main__ import REPORT_STEM

        assert {f"{REPORT_STEM}.json", f"{REPORT_STEM}.md"} <= REPORT_FILENAMES


# ─── Workspace: paths, ownership marker, git ─────────────────────────


class TestWorkspaceDirectory:
    def test_paths(self, tmp_path: Path) -> None:
        workspace = Workspace(tmp_path / "ws")
        assert workspace.mirror == tmp_path / "ws" / "mirror.git"
        assert workspace.clone == tmp_path / "ws" / "base"
        assert workspace.venv == tmp_path / "ws" / "venv"
        assert workspace.logs == tmp_path / "ws" / "logs"
        assert workspace.cache == tmp_path / "ws" / "gco-cache"
        assert workspace.gco == tmp_path / "ws" / "venv" / "bin" / "gco"
        assert workspace.python == tmp_path / "ws" / "venv" / "bin" / "python"

    def test_reset_creates_a_marked_private_workspace_and_clears_its_own(
        self, tmp_path: Path
    ) -> None:
        workspace = Workspace(tmp_path / "ws")
        assert not workspace.owned_by(_RUN_ID)
        workspace_module.reset_workspace(workspace, _RUN_ID)
        assert workspace.owned_by(_RUN_ID) and not workspace.owned_by("other")
        assert workspace.logs.is_dir()
        assert workspace.root.stat().st_mode & 0o777 == 0o700
        (workspace.root / "leftover").write_text("x", encoding="utf-8")
        workspace_module.reset_workspace(workspace, _RUN_ID)
        assert not (workspace.root / "leftover").exists()

    def test_a_directory_without_this_runs_marker_is_never_cleared(self, tmp_path: Path) -> None:
        workspace = Workspace(tmp_path / "ws")
        workspace.root.mkdir()
        (workspace.root / "precious").write_text("x", encoding="utf-8")
        with pytest.raises(RuntimeError, match="is not this run's workspace"):
            workspace_module.reset_workspace(workspace, _RUN_ID)
        with pytest.raises(RuntimeError, match="Refusing to delete"):
            workspace_module.remove_workspace(workspace, _RUN_ID)
        assert (workspace.root / "precious").exists()

    def test_remove_deletes_only_this_runs_workspace(self, tmp_path: Path) -> None:
        workspace = Workspace(tmp_path / "ws")
        assert workspace_module.remove_workspace(workspace, _RUN_ID) is False
        workspace_module.reset_workspace(workspace, _RUN_ID)
        assert workspace_module.remove_workspace(workspace, _RUN_ID) is True
        assert not workspace.root.exists()

    @pytest.mark.parametrize(
        ("stdout", "stderr", "message"),
        [("", "fatal: bad", "fatal: bad"), ("out", "", "out"), ("", "", "unknown git error")],
    )
    def test_git_failures_name_the_command_and_its_message(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        stdout: str,
        stderr: str,
        message: str,
    ) -> None:
        monkeypatch.setattr(
            workspace_module.subprocess,
            "run",
            lambda argv, **_kwargs: subprocess.CompletedProcess(argv, 1, stdout, stderr),
        )
        with pytest.raises(RuntimeError, match=f"git status failed in .*: {message}"):
            workspace_module._git(tmp_path, "status")


class TestPrivateMirrorAndClone:
    def _build(self, tmp_path: Path, repo: Repo) -> Workspace:
        workspace = Workspace(tmp_path / "ws")
        workspace_module.reset_workspace(workspace, _RUN_ID)
        evidence = workspace_module.build_mirror(
            workspace,
            source=repo.root,
            branch="feature",
            candidate_sha=repo.head,
            base_ref="v1.0.0",
            base_commit=repo.base,
        )
        assert evidence["path"] == str(workspace.mirror)
        assert evidence["objects_from"].endswith("objects")
        return workspace

    def test_the_mirror_holds_only_the_base_tag_and_the_candidate(
        self, tmp_path: Path, repo: Repo
    ) -> None:
        _git(repo.root, "tag", "v0.9.0", repo.base)
        workspace = self._build(tmp_path, repo)
        refs = _git(workspace.mirror, "for-each-ref", "--format=%(refname)").split()
        assert sorted(refs) == ["refs/heads/candidate", "refs/tags/v1.0.0"]
        # Objects are borrowed through alternates, never copied.
        assert (workspace.mirror / "objects" / "info" / "alternates").read_text(
            encoding="utf-8"
        ).strip() == str((repo.root / ".git" / "objects").resolve())
        assert "v1.0.1" not in _git(repo.root, "tag", "--list")

    def test_a_moved_branch_or_tag_is_refused(self, tmp_path: Path, repo: Repo) -> None:
        workspace = Workspace(tmp_path / "ws")
        workspace_module.reset_workspace(workspace, _RUN_ID)
        with pytest.raises(RuntimeError, match="not the validated"):
            workspace_module.build_mirror(
                workspace,
                source=repo.root,
                branch="feature",
                candidate_sha=repo.base,
                base_ref="v1.0.0",
                base_commit=repo.base,
            )
        workspace_module.reset_workspace(workspace, _RUN_ID)
        with pytest.raises(RuntimeError, match=r"v1\.0\.0 now names"):
            workspace_module.build_mirror(
                workspace,
                source=repo.root,
                branch="feature",
                candidate_sha=repo.head,
                base_ref="v1.0.0",
                base_commit=repo.head,
            )

    def test_the_clone_is_a_detached_checkout_of_the_base_release(
        self, tmp_path: Path, repo: Repo
    ) -> None:
        workspace = self._build(tmp_path, repo)
        evidence = workspace_module.clone_base(workspace, base_ref="v1.0.0", base_commit=repo.base)
        assert evidence == {"path": str(workspace.clone), "head": repo.base}
        assert (workspace.clone / "VERSION").read_text(encoding="utf-8") == "1.0.0\n"
        assert _git(workspace.clone, "remote", "get-url", "origin") == str(workspace.mirror)
        assert workspace_module.tracked_changes(workspace) == []
        assert (workspace.clone / "cdk.json").stat().st_mode & 0o777 == 0o644

    def test_a_clone_at_another_commit_is_refused(self, tmp_path: Path, repo: Repo) -> None:
        workspace = self._build(tmp_path, repo)
        with pytest.raises(RuntimeError, match="The base clone is at"):
            workspace_module.clone_base(workspace, base_ref="v1.0.0", base_commit=repo.head)

    def test_the_synthetic_tag_lives_only_in_the_mirror(self, tmp_path: Path, repo: Repo) -> None:
        workspace = self._build(tmp_path, repo)
        workspace_module.clone_base(workspace, base_ref="v1.0.0", base_commit=repo.base)
        tag = workspace_module.synthetic_release_tag("v1.0.0")
        assert tag == "v1.0.1"
        expected = {"tag": tag, "commit": repo.head, "repository": str(workspace.mirror)}
        assert (
            workspace_module.tag_candidate(workspace, tag=tag, candidate_sha=repo.head) == expected
        )
        assert (
            workspace_module.tag_candidate(workspace, tag=tag, candidate_sha=repo.head) == expected
        )
        with pytest.raises(RuntimeError, match=r"names .*, not"):
            workspace_module.tag_candidate(workspace, tag=tag, candidate_sha=repo.base)
        _git(workspace.clone, "fetch", "--quiet", "--tags", "origin")
        assert _git(workspace.clone, "rev-parse", f"{tag}^{{commit}}") == repo.head
        assert tag not in _git(repo.root, "tag", "--list")

    def test_synthetic_tags_need_a_release_shaped_base(self) -> None:
        assert workspace_module.synthetic_release_tag("v8.8.0") == "v8.8.1"
        with pytest.raises(ValueError, match="is not a release tag"):
            workspace_module.synthetic_release_tag("main")


class TestRunScopedCdkJson:
    def test_the_run_tag_and_context_are_added_to_the_existing_tags(self, tmp_path: Path) -> None:
        (tmp_path / "cdk.json").write_text(_cdk_json(note="ü"), encoding="utf-8")
        evidence = workspace_module.write_run_context(
            tmp_path,
            run_tag_key="GcoLiveValidationRun",
            run_id=_RUN_ID,
            context={"feature": "true"},
        )
        text = (tmp_path / "cdk.json").read_text(encoding="utf-8")
        context = json.loads(text)["context"]
        assert context["tags"] == {"Project": "GCO", "GcoLiveValidationRun": _RUN_ID}
        assert context["feature"] == "true"
        assert '"note": "ü"' in text and text.endswith("}\n")
        assert evidence == {
            "path": str(tmp_path / "cdk.json"),
            "sha256": workspace_module.sha256_file(tmp_path / "cdk.json"),
            "keys": ["feature"],
        }

    def test_a_cdk_json_without_tags_gets_the_run_tag(self, tmp_path: Path) -> None:
        (tmp_path / "cdk.json").write_text(json.dumps({"context": {}}), encoding="utf-8")
        workspace_module.write_run_context(
            tmp_path, run_tag_key="GcoLiveValidationRun", run_id=_RUN_ID, context={}
        )
        assert json.loads((tmp_path / "cdk.json").read_text(encoding="utf-8"))["context"] == {
            "tags": {"GcoLiveValidationRun": _RUN_ID}
        }

    @pytest.mark.parametrize(
        ("document", "match"),
        [
            ([], "has no context object"),
            ({}, "has no context object"),
            ({"context": {"tags": ["Project"]}}, "must be an object"),
        ],
    )
    def test_malformed_documents_are_refused(
        self, tmp_path: Path, document: Any, match: str
    ) -> None:
        (tmp_path / "cdk.json").write_text(json.dumps(document), encoding="utf-8")
        with pytest.raises(RuntimeError, match=match):
            workspace_module.write_run_context(
                tmp_path, run_tag_key="GcoLiveValidationRun", run_id=_RUN_ID, context={}
            )


class TestCloudAssembly:
    def _write(self, tmp_path: Path, document: Any) -> Path:
        (tmp_path / "cdk.out").mkdir(exist_ok=True)
        (tmp_path / "cdk.out" / "manifest.json").write_text(json.dumps(document), encoding="utf-8")
        return tmp_path

    def test_stack_artifacts_are_read_with_their_tags(self, tmp_path: Path) -> None:
        clone = self._write(
            tmp_path,
            {
                "artifacts": {
                    "Tree": {"type": "cdk:tree"},
                    "odd": "not an object",
                    "gco-live-global": {
                        "type": "aws:cloudformation:stack",
                        "environment": "aws://unknown-account/us-east-2",
                        "properties": {"tags": {"Project": "GCO", "GcoLiveValidationRun": "r"}},
                    },
                    "RegionalStack": {
                        "type": "aws:cloudformation:stack",
                        "properties": {"stackName": "gco-live-us-east-1"},
                        "metadata": {
                            "/RegionalStack": [
                                {"type": "aws:cdk:logicalId", "data": "x"},
                                {
                                    "type": "aws:cdk:stack-tags",
                                    "data": [{"Key": "GcoLiveValidationRun", "Value": "r"}],
                                },
                            ]
                        },
                    },
                    "Untagged": {"type": "aws:cloudformation:stack"},
                }
            },
        )
        assert workspace_module.read_cloud_assembly(clone) == {
            "gco-live-global": {
                "environment": "aws://unknown-account/us-east-2",
                "tags": {"Project": "GCO", "GcoLiveValidationRun": "r"},
            },
            "gco-live-us-east-1": {"environment": "", "tags": {"GcoLiveValidationRun": "r"}},
            "Untagged": {"environment": "", "tags": {}},
        }

    def test_an_unreadable_or_empty_manifest_fails(self, tmp_path: Path) -> None:
        with pytest.raises(RuntimeError, match="Could not read the base cloud assembly"):
            workspace_module.read_cloud_assembly(tmp_path)
        (tmp_path / "cdk.out").mkdir()
        (tmp_path / "cdk.out" / "manifest.json").write_text("{", encoding="utf-8")
        with pytest.raises(RuntimeError, match="Could not read the base cloud assembly"):
            workspace_module.read_cloud_assembly(tmp_path)
        for document in ([], {"artifacts": []}):
            self._write(tmp_path, document)
            with pytest.raises(RuntimeError, match="lists no artifacts"):
                workspace_module.read_cloud_assembly(tmp_path)


class TestCommandEnvironment:
    def test_the_base_gco_gets_its_venv_its_cache_and_the_harness_identity(
        self, tmp_path: Path
    ) -> None:
        workspace = Workspace(tmp_path / "ws")
        base = {
            "PATH": "/usr/bin",
            "PYTHONPATH": "/elsewhere",
            "VIRTUAL_ENV": "/other-venv",
            "GIT_DIR": "/other/.git",
            "GCO_OUTPUT_FORMAT": "yaml",
            "GCO_PROJECT_NAME": "someone-else",
            "AWS_PROFILE": "validation",
        }
        environment = workspace_module.command_environment(
            workspace, identity={"GCO_PROJECT_NAME": _PROJECT}, base=base
        )
        assert environment["PATH"] == f"{workspace.venv / 'bin'}:/usr/bin"
        assert environment["VIRTUAL_ENV"] == str(workspace.venv)
        assert environment["GCO_CACHE_DIR"] == str(workspace.cache)
        assert environment["GCO_PROJECT_NAME"] == _PROJECT
        assert environment["AWS_PROFILE"] == "validation"
        assert environment["PYTHONNOUSERSITE"] == "1"
        for key in ("PYTHONPATH", "GIT_DIR", "GCO_OUTPUT_FORMAT"):
            assert key not in environment

    def test_without_a_workspace_only_the_scrubbing_applies(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("PYTHONPATH", "/elsewhere")
        environment = workspace_module.command_environment(None)
        assert "PYTHONPATH" not in environment
        assert environment["PIP_DISABLE_PIP_VERSION_CHECK"] == "1"
        assert workspace_module.command_environment(Workspace(Path("/ws")), base={})["PATH"] == str(
            Path("/ws/venv/bin")
        )


# ─── Running commands ────────────────────────────────────────────────


class TestRunLogged:
    def test_output_is_logged_echoed_and_stdout_kept(self, tmp_path: Path) -> None:
        echoed: list[str] = []
        result = workspace_module.run_logged(
            [
                sys.executable,
                "-c",
                "import sys; print('out one'); print('err one', file=sys.stderr); print('out two')",
            ],
            cwd=tmp_path,
            env=workspace_module.command_environment(None),
            log_path=tmp_path / "step.log",
            timeout_seconds=60,
            echo=echoed.append,
        )
        assert result.ok and result.exit_code == 0 and not result.timed_out
        assert result.stdout == "out one\nout two\n"
        assert sorted(result.tail) == ["err one", "out one", "out two"]
        assert sorted(echoed) == ["err one\n", "out one\n", "out two\n"]
        log = (tmp_path / "step.log").read_text(encoding="utf-8")
        assert log.startswith(f"$ {sys.executable} -c ")
        assert "out two" in log and "err one" in log
        assert result.to_dict() == {
            "argv": list(result.argv),
            "exit_code": 0,
            "duration_seconds": result.duration_seconds,
            "timed_out": False,
            "log": str(tmp_path / "step.log"),
            "tail": list(result.tail),
        }

    def test_commands_run_under_umask_022(self, tmp_path: Path) -> None:
        workspace_module.run_logged(
            [sys.executable, "-c", "open('made', 'w').close()"],
            cwd=tmp_path,
            env=workspace_module.command_environment(None),
            log_path=tmp_path / "step.log",
            timeout_seconds=60,
        )
        assert (tmp_path / "made").stat().st_mode & 0o777 == 0o644

    def test_a_failing_command_reports_its_exit_code(self, tmp_path: Path) -> None:
        result = workspace_module.run_logged(
            [sys.executable, "-c", "raise SystemExit(3)"],
            cwd=tmp_path,
            env=workspace_module.command_environment(None),
            log_path=tmp_path / "step.log",
            timeout_seconds=60,
        )
        assert result.exit_code == 3 and not result.ok

    def test_a_command_past_its_timeout_is_stopped(self, tmp_path: Path) -> None:
        result = workspace_module.run_logged(
            [sys.executable, "-c", "import time; print('started', flush=True); time.sleep(60)"],
            cwd=tmp_path,
            env=workspace_module.command_environment(None),
            log_path=tmp_path / "step.log",
            timeout_seconds=1.0,
            grace_seconds=5.0,
        )
        assert result.timed_out and not result.ok
        assert result.exit_code == -signal.SIGTERM
        assert result.tail == ("started",)

    def test_a_command_ignoring_sigterm_is_killed_after_the_grace(self, tmp_path: Path) -> None:
        script = (
            "import signal, time; signal.signal(signal.SIGTERM, signal.SIG_IGN); "
            "print('ready', flush=True); time.sleep(60)"
        )
        result = workspace_module.run_logged(
            [sys.executable, "-c", script],
            cwd=tmp_path,
            env=workspace_module.command_environment(None),
            log_path=tmp_path / "step.log",
            timeout_seconds=1.0,
            grace_seconds=0.5,
        )
        assert result.timed_out and result.exit_code == -signal.SIGKILL

    def test_an_interrupted_harness_stops_the_command_and_reraises(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        started: list[subprocess.Popen[str]] = []
        real_popen = subprocess.Popen

        class InterruptingPopen(real_popen):
            def __init__(self, *args: Any, **kwargs: Any) -> None:
                super().__init__(*args, **kwargs)
                started.append(self)
                self._interrupted = False

            def wait(self, timeout: float | None = None) -> int:
                if not self._interrupted:
                    self._interrupted = True
                    raise KeyboardInterrupt
                return int(super().wait(timeout))

        monkeypatch.setattr(workspace_module.subprocess, "Popen", InterruptingPopen)
        with pytest.raises(KeyboardInterrupt):
            workspace_module.run_logged(
                [sys.executable, "-c", "import time; time.sleep(60)"],
                cwd=tmp_path,
                env=workspace_module.command_environment(None),
                log_path=tmp_path / "step.log",
                timeout_seconds=60,
                grace_seconds=5.0,
            )
        (process,) = started
        assert process.returncode == -signal.SIGTERM

    def test_the_process_id_is_reported_as_soon_as_the_command_starts(self, tmp_path: Path) -> None:
        started: list[int] = []
        result = workspace_module.run_logged(
            [sys.executable, "-c", "import os; print(os.getpid())"],
            cwd=tmp_path,
            env=workspace_module.command_environment(None),
            log_path=tmp_path / "step.log",
            timeout_seconds=60,
            on_start=started.append,
        )
        assert started == [int(result.stdout)]

    def test_a_running_command_line_is_found_by_process_id(self, tmp_path: Path) -> None:
        import os

        command = workspace_module.running_command(os.getpid())
        assert command is not None and "python" in command.lower()
        finished = subprocess.Popen([sys.executable, "-c", "pass"])
        finished.wait()
        assert workspace_module.running_command(finished.pid) is None

    def test_terminating_a_finished_command_is_a_no_op(self, tmp_path: Path) -> None:
        process = subprocess.Popen([sys.executable, "-c", "pass"], start_new_session=True)
        process.wait()
        workspace_module._terminate(process, grace_seconds=0.1)
        assert process.returncode == 0

    def test_a_process_that_outlives_sigkill_is_given_up_on(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        sent: list[int] = []
        monkeypatch.setattr(workspace_module.os, "killpg", lambda _pid, signum: sent.append(signum))

        class Stuck:
            pid = 12345

            def wait(self, timeout: float | None = None) -> int:
                raise subprocess.TimeoutExpired("stuck", timeout or 0)

        workspace_module._terminate(Stuck(), grace_seconds=0)  # type: ignore[arg-type]
        assert sent == [signal.SIGTERM, signal.SIGKILL]

    def test_console_echo_prefixes_each_line(self, capsys: pytest.CaptureFixture[str]) -> None:
        echo = workspace_module.console_echo("base deploy")
        echo("one\n")
        echo("two")
        assert capsys.readouterr().out == "[base deploy] one\n[base deploy] two\n"


class TestResultDocuments:
    def test_the_last_document_after_streamed_output_is_returned(self) -> None:
        text = "CDK progress\n{ not json\n" + json.dumps({"status": "ok", "a": 1}, indent=2) + "\n"
        assert workspace_module.final_json_document(text) == {"status": "ok", "a": 1}
        assert workspace_module.final_json_document("no document\n{ broken\n") is None
        assert workspace_module.final_json_document("") is None

    def test_a_wrapped_document_is_unwrapped(self) -> None:
        inner = json.dumps({"status": "ok", "plan": {"target": "v1.0.1"}}, indent=2)
        wrapped = json.dumps({"status": "ok", "output": "building...\n" + inner}, indent=2)
        assert workspace_module.gco_result_document(wrapped) == json.loads(inner)
        plain_output = json.dumps({"status": "ok", "output": "just text"}, indent=2)
        assert workspace_module.gco_result_document(plain_output) == {
            "status": "ok",
            "output": "just text",
        }
        assert workspace_module.gco_result_document("nothing") is None
        assert workspace_module.gco_result_document(inner) == json.loads(inner)


# ─── Sentinel ────────────────────────────────────────────────────────


class _Clock:
    def __init__(self) -> None:
        self.now = 0.0
        self.sleeps: list[float] = []

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


def _sentinel_ctx(tmp_path: Path, answers: list[Any]) -> Any:
    ctx = _ctx(_settings(tmp_path))
    ctx.aws_client.call_api.side_effect = answers
    return ctx


def _stored(run_id: str = _RUN_ID, created_at: str = "2026-09-29T12:00:00+00:00") -> dict[str, Any]:
    body = sentinel_module.sentinel_body(run_id)
    return {**body, "created_at": created_at, "updated_at": created_at}


class TestSentinel:
    def test_the_name_is_stable_and_fits_the_api_limit(self) -> None:
        name = sentinel_module.sentinel_name(_RUN_ID)
        assert name == sentinel_module.sentinel_name(_RUN_ID) != sentinel_module.sentinel_name("x")
        assert len(name) <= 63 and name.startswith("gco-upgrade-sentinel-")
        body = sentinel_module.sentinel_body(_RUN_ID)
        assert body["name"] == name and body["manifest"]["kind"] == "Job"

    def test_calls_retry_while_the_api_is_not_answering(self, tmp_path: Path) -> None:
        clock = _Clock()
        ctx = _sentinel_ctx(
            tmp_path,
            [
                APIRequestError(503, "Template store not initialized"),
                requests.ConnectionError("reset"),
                {"template": _stored()},
            ],
        )
        created = sentinel_module.create_sentinel(
            ctx, timeout_seconds=600, sleep=clock.sleep, clock=clock
        )
        assert created == _stored()
        assert clock.sleeps == [sentinel_module.RETRY_INTERVAL_SECONDS] * 2
        post = ctx.aws_client.call_api.call_args_list[0].kwargs
        assert post["method"] == "POST" and post["max_attempts"] is None
        assert post["body"] == sentinel_module.sentinel_body(_RUN_ID)

    def test_a_call_gives_up_at_its_deadline(self, tmp_path: Path) -> None:
        clock = _Clock()
        ctx = _sentinel_ctx(tmp_path, [APIRequestError(502, "bad gateway")] * 10)
        with pytest.raises(RuntimeError, match="did not succeed in 2 attempt"):
            sentinel_module.create_sentinel(ctx, timeout_seconds=15, sleep=clock.sleep, clock=clock)

    def test_an_answer_is_not_retried(self, tmp_path: Path) -> None:
        ctx = _sentinel_ctx(tmp_path, [APIRequestError(403, "denied")])
        with pytest.raises(APIRequestError, match="denied"):
            sentinel_module.create_sentinel(ctx, timeout_seconds=600, sleep=pytest.fail)

    def test_a_conflict_is_resolved_by_reading_the_template_back(self, tmp_path: Path) -> None:
        ctx = _sentinel_ctx(tmp_path, [APIRequestError(409, "exists"), {"template": _stored()}])
        assert (
            sentinel_module.create_sentinel(ctx, timeout_seconds=600, sleep=pytest.fail)
            == _stored()
        )
        get = ctx.aws_client.call_api.call_args_list[1].kwargs
        assert get["method"] == "GET" and get["max_attempts"] == 1
        assert get["path"].endswith(sentinel_module.sentinel_name(_RUN_ID))

    @pytest.mark.parametrize(
        ("template", "match"),
        [
            (None, "returned no template record"),
            ({"name": "x"}, "returned no template record"),
            ({**_stored(), "manifest": {}}, r"differs from what was written: \['manifest'\]"),
        ],
    )
    def test_a_missing_or_different_template_fails(
        self, tmp_path: Path, template: Any, match: str
    ) -> None:
        ctx = _sentinel_ctx(tmp_path, [{"template": template}])
        with pytest.raises(RuntimeError, match=match):
            sentinel_module.create_sentinel(ctx, timeout_seconds=600, sleep=pytest.fail)

    def test_verification_requires_the_original_creation_time(self, tmp_path: Path) -> None:
        written = _stored()
        ctx = _sentinel_ctx(
            tmp_path, [{"template": _stored()}, {"template": _stored(created_at="later")}]
        )
        assert sentinel_module.verify_sentinel(ctx, written, timeout_seconds=60) == written
        with pytest.raises(RuntimeError, match="was recreated"):
            sentinel_module.verify_sentinel(ctx, written, timeout_seconds=60)

    def test_deletion_tolerates_an_already_absent_template(self, tmp_path: Path) -> None:
        name = sentinel_module.sentinel_name(_RUN_ID)
        ctx = _sentinel_ctx(
            tmp_path,
            [{"message": "deleted"}, APIRequestError(404, "gone"), APIRequestError(403, "no")],
        )
        assert sentinel_module.delete_sentinel(ctx, timeout_seconds=60) == {
            "name": name,
            "deleted": True,
        }
        assert sentinel_module.delete_sentinel(ctx, timeout_seconds=60) == {
            "name": name,
            "deleted": False,
            "already_absent": True,
        }
        with pytest.raises(APIRequestError):
            sentinel_module.delete_sentinel(ctx, timeout_seconds=60)


# ─── Actions: shared helpers ─────────────────────────────────────────


@pytest.fixture
def prepared(tmp_path: Path, repo: Repo) -> tuple[UpgradeRunSettings, Any, Workspace]:
    """A real prepared workspace for ``repo``, with a stand-in base ``gco``."""
    settings = _settings(tmp_path, repo)
    workspace = Workspace(settings.workspace_dir)
    workspace_module.reset_workspace(workspace, _RUN_ID)
    workspace_module.build_mirror(
        workspace,
        source=repo.root,
        branch="feature",
        candidate_sha=repo.head,
        base_ref="v1.0.0",
        base_commit=repo.base,
    )
    workspace_module.clone_base(workspace, base_ref="v1.0.0", base_commit=repo.base)
    cdk_json = workspace_module.write_run_context(
        workspace.clone,
        run_tag_key="GcoLiveValidationRun",
        run_id=_RUN_ID,
        context=settings.base_cdk_context(),
    )
    workspace.gco.parent.mkdir(parents=True)
    workspace.gco.write_text("#!/bin/sh\n", encoding="utf-8")
    ctx = _ctx(
        settings,
        state={
            "target_stack_regions": dict(_TARGETS),
            actions.STATE_KEY: {
                "workspace": {
                    "prepared_at": "2026-09-29T11:00:00+00:00",
                    "cdk_json": cdk_json,
                    "synthetic_tag": "v1.0.1",
                }
            },
        },
    )
    return settings, ctx, workspace


def _owned(ids: dict[str, str], **extra: Any) -> dict[str, dict[str, dict[str, Any]]]:
    owned: dict[str, dict[str, dict[str, Any]]] = {}
    for name, stack_id in ids.items():
        owned.setdefault(_TARGETS[name], {})[name] = {"stack_id": stack_id, **extra.get(name, {})}
    return owned


_BEFORE = {name: f"arn:stack/{name}/base" for name in _TARGETS}


class TestActionHelpers:
    def test_the_actions_need_upgrade_settings(self) -> None:
        with pytest.raises(TypeError, match="need UpgradeRunSettings"):
            actions._settings(SimpleNamespace(settings=SimpleNamespace()))

    def test_malformed_harness_state_fails_closed(self, tmp_path: Path) -> None:
        ctx = _ctx(_settings(tmp_path), state={actions.STATE_KEY: []})
        with pytest.raises(RuntimeError, match="must be an object"):
            actions._state(ctx)
        for phases in ([], {"deploy": "started"}):
            ctx = _ctx(_settings(tmp_path), state={actions.STATE_KEY: {"phases": phases}})
            with pytest.raises(RuntimeError, match="phases is malformed"):
                actions._phases(ctx)

    def test_the_base_gco_is_pinned_to_the_harness_deployment(self, tmp_path: Path) -> None:
        ctx = _ctx(_settings(tmp_path))
        assert actions._gco_identity(ctx) == {
            "GCO_PROJECT_NAME": _PROJECT,
            "GCO_GLOBAL_REGION": "us-east-2",
            "GCO_API_GATEWAY_REGION": "us-east-2",
            "GCO_MONITORING_REGION": "us-east-2",
            "GCO_DEFAULT_REGION": _REGION,
        }
        environment = actions._environment(ctx)
        assert environment["GCO_PROJECT_NAME"] == _PROJECT
        assert environment["VIRTUAL_ENV"] == str(ctx.settings.workspace_dir / "venv")

    def test_the_server_clock_comes_from_sts_after_the_account_check(self, tmp_path: Path) -> None:
        ctx = _ctx(_settings(tmp_path))
        assert actions._server_time(ctx) == datetime(2026, 9, 29, 12, 0, tzinfo=UTC)
        ctx.session.client.assert_called_with("sts", region_name="us-east-2")
        ctx.session.client.return_value = _sts_client(date="Tue, 29 Sep 2026 12:00:00 -0000")
        assert actions._server_time(ctx) == datetime(2026, 9, 29, 12, 0, tzinfo=UTC)
        ctx.session.client.return_value = _sts_client(account="999999999999")
        with pytest.raises(RuntimeError, match="does not match expected account"):
            actions._server_time(ctx)
        ctx.session.client.return_value = _sts_client(date=None)
        with pytest.raises(RuntimeError, match="no Date header"):
            actions._server_time(ctx)

    def test_steps_run_in_the_workspace_environment_with_a_log(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        ctx = _ctx(_settings(tmp_path))
        seen: dict[str, Any] = {}

        def fake_run_logged(argv: Any, **kwargs: Any) -> CommandResult:
            seen.update(argv=argv, **kwargs)
            return _result()

        monkeypatch.setattr(actions, "run_logged", fake_run_logged)
        actions._run_step(ctx, "base deploy", ["gco"], cwd=tmp_path, timeout_seconds=5)
        assert seen["argv"] == ["gco"] and seen["cwd"] == tmp_path
        assert seen["log_path"] == ctx.settings.workspace_dir / "logs" / "base-deploy.log"
        assert seen["env"]["GCO_PROJECT_NAME"] == _PROJECT
        assert seen["timeout_seconds"] == 5 and callable(seen["echo"])
        # A plain step records nothing; a phase's command checkpoints its PID.
        seen["on_start"](4242)
        ctx.persist.assert_not_called()
        actions._begin_phase(ctx, "deploy")
        actions._run_step(
            ctx, "base deploy", ["gco"], cwd=tmp_path, timeout_seconds=5, phase="deploy"
        )
        seen["on_start"](4242)
        assert ctx.checkpoint.state[actions.STATE_KEY]["phases"]["deploy"]["pid"] == 4242

    def test_targets_are_required(self, tmp_path: Path) -> None:
        for state in ({}, {"target_stack_regions": {}}):
            with pytest.raises(RuntimeError, match="lacks target stack Regions"):
                actions._target_regions(_ctx(_settings(tmp_path), state=state))


class TestPhases:
    @pytest.fixture
    def adoption(self, monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
        calls: list[dict[str, Any]] = []

        def fake_adopt(ctx: Any, **kwargs: Any) -> dict[str, Any]:
            calls.append(kwargs)
            return {"phase": kwargs["phase"], "adopted": []}

        monkeypatch.setattr(actions, "_adopt_run_tagged_stacks", fake_adopt)
        monkeypatch.setattr(
            actions, "_reconcile_stack_ownership", lambda ctx: calls.append({"reconciled": True})
        )
        monkeypatch.setattr(
            actions, "_checkpoint_retained_kms_keys", lambda ctx: [{"arn": "k1"}, {"arn": "k2"}]
        )
        monkeypatch.setattr(
            actions, "prune_local_cdk_asset_images_safely", lambda: {"removed_images": []}
        )
        return calls

    def test_a_deploy_phase_marks_the_deployment_attempted_before_it_runs(
        self, tmp_path: Path
    ) -> None:
        ctx = _ctx(_settings(tmp_path))
        phase = actions._begin_phase(ctx, "deploy", note="x")
        assert phase["window_started_at"] == "2026-09-29T12:00:00+00:00"
        assert phase["note"] == "x" and phase["started_at"]
        assert ctx.checkpoint.deployment_attempted is True and ctx.checkpoint.destroyed is False
        assert ctx.checkpoint.state[actions.STATE_KEY]["phases"]["deploy"] is phase
        ctx.persist.assert_called()

    def test_other_phases_leave_the_deployment_flags_alone(self, tmp_path: Path) -> None:
        ctx = _ctx(_settings(tmp_path))
        ctx.checkpoint.destroyed = True
        actions._begin_phase(ctx, "upgrade")
        assert ctx.checkpoint.deployment_attempted is False and ctx.checkpoint.destroyed is True

    def test_only_the_upgrade_may_replace_its_workload_stacks(self) -> None:
        assert actions._replaceable("deploy", {"plan": {"workload_stacks": _WORKLOAD}}) == ()
        assert actions._replaceable("upgrade", {"plan": {"workload_stacks": _WORKLOAD}}) == tuple(
            _WORKLOAD
        )
        assert actions._replaceable("upgrade", {}) == ()

    def test_settling_a_phase_adopts_reconciles_and_checkpoints_retained_resources(
        self, tmp_path: Path, adoption: list[dict[str, Any]]
    ) -> None:
        ctx = _ctx(_settings(tmp_path))
        actions._begin_phase(ctx, "upgrade", plan={"workload_stacks": _WORKLOAD})
        actions._settle_phase(ctx, "upgrade")
        phase = ctx.checkpoint.state[actions.STATE_KEY]["phases"]["upgrade"]
        assert adoption[0] == {
            "phase": "upgrade",
            "window_started_at": datetime(2026, 9, 29, 12, 0, tzinfo=UTC),
            "replaceable": tuple(_WORKLOAD),
        }
        assert adoption[1] == {"reconciled": True}
        assert phase["adoption"] == {"phase": "upgrade", "adopted": []}
        assert phase["owned_kms_keys"] == 2
        assert phase["local_image_prune"] == {"removed_images": []}
        assert phase["finished_at"]

    def test_an_interrupted_phase_is_adopted_and_marked(
        self, tmp_path: Path, adoption: list[dict[str, Any]]
    ) -> None:
        ctx = _ctx(_settings(tmp_path))
        actions._begin_phase(ctx, "deploy")
        actions._settle_phase(ctx, "deploy")
        actions._begin_phase(ctx, "upgrade")
        adoption.clear()
        assert actions._recover_interrupted_phases(ctx) == ["upgrade"]
        phases = ctx.checkpoint.state[actions.STATE_KEY]["phases"]
        assert phases["upgrade"]["interrupted"] is True and phases["upgrade"]["finished_at"]
        assert "interrupted" not in phases["deploy"]
        assert [call["phase"] for call in adoption] == ["upgrade"]
        assert actions._recover_interrupted_phases(ctx) == []

    def test_nothing_is_adopted_while_an_orphaned_command_still_runs(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, adoption: list[dict[str, Any]]
    ) -> None:
        ctx = _ctx(_settings(tmp_path))
        gco = str(Workspace(ctx.settings.workspace_dir).gco)
        phase = actions._begin_phase(ctx, "upgrade")
        phase["pid"] = 4242
        commands = {4242: f"{sys.executable} {gco} -o json upgrade --yes"}
        monkeypatch.setattr(actions, "running_command", lambda pid: commands.get(pid))
        with pytest.raises(RuntimeError, match=r"still running .*kill -TERM -4242"):
            actions._recover_interrupted_phases(ctx)
        assert adoption == []
        # The process ID now belongs to something else: the command is gone.
        commands[4242] = "/usr/bin/vim notes.txt"
        assert actions._recover_interrupted_phases(ctx) == ["upgrade"]
        actions._begin_phase(ctx, "deploy")["pid"] = "not-a-pid"
        assert actions._recover_interrupted_phases(ctx) == ["deploy"]

    @pytest.mark.parametrize(
        ("phase", "match"),
        [
            ({"interrupted": True}, "was interrupted before it finished"),
            (
                {"command": {"timed_out": True, "exit_code": -15, "log": "x.log"}},
                "timed out; see x.log",
            ),
            ({"command": {"exit_code": 2, "log": "x.log"}}, "exited with 2; see x.log"),
            ({}, "exited with None"),
        ],
    )
    def test_a_phase_fails_unless_its_command_succeeded(
        self, phase: dict[str, Any], match: str
    ) -> None:
        with pytest.raises(ActionFailure, match=match) as raised:
            actions._phase_outcome(phase, action="deploy", command="gco stacks deploy-all")
        assert raised.value.details["phase"] == phase
        actions._phase_outcome({"command": {"exit_code": 0}}, action="deploy", command="x")


class TestPreparedWorkspace:
    def test_a_prepared_workspace_is_accepted(self, prepared: Any) -> None:
        _unused, ctx, _workspace = prepared
        record = actions._require_prepared_workspace(ctx)
        assert record["synthetic_tag"] == "v1.0.1"

    def test_the_workspace_must_have_been_prepared(self, prepared: Any) -> None:
        _unused, ctx, _workspace = prepared
        ctx.checkpoint.state[actions.STATE_KEY]["workspace"] = {"cdk_json": {}}
        with pytest.raises(RuntimeError, match="prepare action has not completed"):
            actions._require_prepared_workspace(ctx)

    def test_the_workspace_must_still_be_there(self, prepared: Any) -> None:
        _unused, ctx, workspace = prepared
        workspace.gco.unlink()
        with pytest.raises(RuntimeError, match="missing or incomplete"):
            actions._require_prepared_workspace(ctx)

    def test_the_clone_must_still_be_at_the_base_release(self, prepared: Any, repo: Repo) -> None:
        _unused, ctx, workspace = prepared
        _git(workspace.clone, "fetch", "--quiet", "origin", "candidate")
        _git(workspace.clone, "checkout", "--quiet", "--detach", repo.head)
        with pytest.raises(RuntimeError, match=r"not v1\.0\.0"):
            actions._require_prepared_workspace(ctx)

    def test_only_cdk_json_may_differ_and_only_as_written(self, prepared: Any) -> None:
        _unused, ctx, workspace = prepared
        (workspace.clone / "VERSION").write_text("edited\n", encoding="utf-8")
        with pytest.raises(
            RuntimeError, match=re.escape("changed beyond the run's cdk.json: VERSION, cdk.json")
        ):
            actions._require_prepared_workspace(ctx)
        _git(workspace.clone, "checkout", "--", "VERSION", "cdk.json")
        with pytest.raises(RuntimeError, match=r"none \(cdk.json was reverted\)"):
            actions._require_prepared_workspace(ctx)
        (workspace.clone / "cdk.json").write_text("{}\n", encoding="utf-8")
        with pytest.raises(
            RuntimeError, match=re.escape("cdk.json changed after the run wrote it")
        ):
            actions._require_prepared_workspace(ctx)


class TestStackChecks:
    def test_generations_are_read_from_the_owned_records(self, tmp_path: Path) -> None:
        ctx = _ctx(
            _settings(tmp_path),
            state={
                "target_stack_regions": dict(_TARGETS),
                "owned_stacks": _owned({_CONTROL[0]: "id-0"}),
            },
        )
        generations = actions._stack_generations(ctx)
        assert generations[_CONTROL[0]] == "id-0"
        assert generations[_WORKLOAD[0]] == ""

    def test_every_target_must_stand_under_the_owned_id(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        owned = _owned(_BEFORE)
        ctx = _ctx(
            _settings(tmp_path),
            state={"target_stack_regions": dict(_TARGETS), "owned_stacks": owned},
        )
        live = {
            name: {"stack_id": stack_id, "status": "CREATE_COMPLETE"}
            for name, stack_id in _BEFORE.items()
        }
        monkeypatch.setattr(
            actions, "describe_stack", lambda _session, _region, name: live.get(name)
        )
        stacks = actions._require_healthy_targets(ctx)
        assert stacks[_CONTROL[0]] == {
            "region": "us-east-2",
            "stack_id": _BEFORE[_CONTROL[0]],
            "status": "CREATE_COMPLETE",
        }
        live[_CONTROL[0]] = {"stack_id": "other", "status": "UPDATE_COMPLETE"}
        live[_CONTROL[1]]["status"] = "ROLLBACK_COMPLETE"
        del live[_CONTROL[2]]
        del owned[_REGION][_WORKLOAD[0]]
        with pytest.raises(ActionFailure, match="not deployed and owned") as raised:
            actions._require_healthy_targets(ctx)
        message = str(raised.value)
        assert f"{_CONTROL[0]} (UPDATE_COMPLETE)" in message
        assert f"{_CONTROL[1]} (ROLLBACK_COMPLETE)" in message
        assert f"{_CONTROL[2]} (absent)" in message
        assert f"{_WORKLOAD[0]} (CREATE_COMPLETE)" in message
        assert _WORKLOAD[1] not in message


# ─── preflight ───────────────────────────────────────────────────────


class TestVerifyBaseRelease:
    def test_the_base_release_is_pinned_and_deploys_this_topology(
        self, tmp_path: Path, repo: Repo
    ) -> None:
        ctx = _ctx(_settings(tmp_path, repo))
        assert actions._verify_base_release(ctx) == {
            "ref": "v1.0.0",
            "commit": repo.base,
            "project_name": _PROJECT,
            "deployment_regions": _REGIONS,
            "image_mirror": False,
        }

    def test_a_base_release_with_the_image_mirror_on_is_accepted(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Every release ships the mirror on; deploy checks its repositories.
        settings = _settings(tmp_path)
        replies = {
            "rev-parse": settings.base_commit,
            "merge-base": settings.base_commit,
            "show": _cdk_json(volcano_image_mirror={"enabled": True}),
        }
        monkeypatch.setattr(
            actions, "_run_git", lambda _root, command, *_args, **_kwargs: replies[command]
        )
        assert actions._verify_base_release(_ctx(settings))["image_mirror"] is True

    @pytest.mark.parametrize(
        ("answers", "match"),
        [
            ({"rev-parse": ""}, "names no commit in this repository"),
            ({"rev-parse": "c" * 40}, "names c{40} in this repository"),
            ({"merge-base": "d" * 40}, "does not descend from v1.0.0"),
            ({"show": "{not json"}, "is not valid JSON"),
            ({"show": "[]"}, "has no context object"),
            (
                {
                    "show": json.dumps(
                        {"context": {"project_name": "other", "deployment_regions": _REGIONS}}
                    )
                },
                "deploys a different project_name than",
            ),
            (
                {
                    "show": json.dumps(
                        {"context": {"project_name": _PROJECT, "deployment_regions": {}}}
                    )
                },
                "different deployment_regions",
            ),
        ],
    )
    def test_a_moved_or_mismatched_base_is_refused(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        answers: dict[str, str],
        match: str,
    ) -> None:
        settings = _settings(tmp_path)
        defaults = {
            "rev-parse": settings.base_commit,
            "merge-base": settings.base_commit,
            "show": _cdk_json(),
        }
        replies = {**defaults, **answers}
        monkeypatch.setattr(
            actions, "_run_git", lambda _root, command, *_args, **_kwargs: replies[command]
        )
        with pytest.raises(RuntimeError, match=match):
            actions._verify_base_release(_ctx(settings))


class TestCandidateStackTags:
    def test_the_checkouts_app_turns_cdk_json_tags_into_stack_tags(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        ctx = _ctx(_settings(tmp_path))
        ctx.cdk_context["tags"] = {"Project": "GCO", "Tier": 1}
        assembly = {name: {"tags": {"Project": "GCO", "Tier": "1"}} for name in _TARGETS}
        monkeypatch.setattr(actions, "read_cloud_assembly", lambda root: assembly)
        assert actions._verify_candidate_stack_tags(ctx) == {
            "cdk_json_tags": ["Project", "Tier"],
            "stacks": len(_TARGETS),
        }
        assembly[_WORKLOAD[1]]["tags"] = {"Project": "GCO"}
        del assembly[_CONTROL[0]]
        with pytest.raises(
            RuntimeError, match=re.escape("does not apply cdk.json context.tags")
        ) as raised:
            actions._verify_candidate_stack_tags(ctx)
        assert f"{_CONTROL[0]}, {_WORKLOAD[1]}" in str(raised.value)

    @pytest.mark.parametrize("tags", [None, {}, ["Project"]])
    def test_without_cdk_json_tags_nothing_proves_the_run_tag_survives(
        self, tmp_path: Path, tags: Any
    ) -> None:
        ctx = _ctx(_settings(tmp_path))
        ctx.cdk_context["tags"] = tags
        with pytest.raises(RuntimeError, match=re.escape("context.tags is empty")):
            actions._verify_candidate_stack_tags(ctx)


class TestPreflightChecks:
    def test_the_tools_the_base_toolchain_needs(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(actions.shutil, "which", lambda name: f"/bin/{name}")
        assert actions._required_tools() == {
            "git": "/bin/git",
            "node": "/bin/node",
            "npm": "/bin/npm",
        }
        monkeypatch.setattr(
            actions.shutil, "which", lambda name: None if name != "git" else "/bin/git"
        )
        with pytest.raises(RuntimeError, match="needs node, npm on PATH"):
            actions._required_tools()

    def test_the_workspace_volume_must_clear_the_floor(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        assert actions._check_workspace_disk(_settings(tmp_path)) == "not-required"
        settings = _settings(tmp_path, min_free_disk_gib=20)
        probed: list[Path] = []

        def usage(path: Path) -> Any:
            probed.append(path)
            return SimpleNamespace(free=25.5 * 1024**3)

        monkeypatch.setattr(actions.shutil, "disk_usage", usage)
        assert actions._check_workspace_disk(settings) == 25.5
        # The workspace does not exist yet: the nearest existing parent is probed.
        assert probed == [tmp_path / "reports"] or probed == [tmp_path]
        monkeypatch.setattr(
            actions.shutil, "disk_usage", lambda _path: SimpleNamespace(free=3 * 1024**3)
        )
        with pytest.raises(
            RuntimeError, match=re.escape("has 3.0 GiB free, below the 20 GiB floor")
        ):
            actions._check_workspace_disk(settings)

    @pytest.fixture
    def preflight(self, monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
        calls: dict[str, Any] = {"recovered": 0}
        monkeypatch.setattr(actions, "action_preflight", lambda ctx: {"account": _ACCOUNT})
        monkeypatch.setattr(actions, "_verify_base_release", lambda ctx: {"ref": "v1.0.0"})
        monkeypatch.setattr(actions, "_verify_candidate_stack_tags", lambda ctx: {"stacks": 5})
        monkeypatch.setattr(actions, "_required_tools", lambda: {"git": "/bin/git"})
        monkeypatch.setattr(actions, "_check_workspace_disk", lambda settings: 40.0)

        def recover(ctx: Any) -> list[str]:
            calls["recovered"] += 1
            return ["upgrade"]

        monkeypatch.setattr(actions, "_recover_interrupted_phases", recover)
        return calls

    def test_a_fresh_preflight_pins_the_base_release(
        self, tmp_path: Path, preflight: dict[str, Any]
    ) -> None:
        ctx = _ctx(_settings(tmp_path))
        details = actions.action_upgrade_preflight(ctx)
        assert details == {
            "account": _ACCOUNT,
            "base_release": {"ref": "v1.0.0"},
            "candidate_stack_tags": {"stacks": 5},
            "synthetic_tag": "v1.0.1",
            "tools": {"git": "/bin/git"},
            "workspace": str(ctx.settings.workspace_dir),
            "workspace_free_disk_gib": 40.0,
            "recovered_phases": [],
        }
        assert ctx.checkpoint.state[actions.STATE_KEY]["base"] == {"ref": "v1.0.0"}
        assert preflight["recovered"] == 0

    def test_a_resumed_preflight_adopts_interrupted_phases_first(
        self, tmp_path: Path, preflight: dict[str, Any]
    ) -> None:
        ctx = _ctx(
            _settings(tmp_path),
            deployment_attempted=True,
            selected=("preflight", "baseline"),
            state={
                "target_stack_regions": dict(_TARGETS),
                actions.STATE_KEY: {"base": {"ref": "v1.0.0"}},
            },
        )
        details = actions.action_upgrade_preflight(ctx)
        assert details["recovered_phases"] == ["upgrade"] and preflight["recovered"] == 1
        assert details["tools"] == "not-required" and details["workspace_free_disk_gib"] is None

    def test_a_changed_base_release_refuses_the_resume(
        self, tmp_path: Path, preflight: dict[str, Any]
    ) -> None:
        ctx = _ctx(
            _settings(tmp_path),
            state={
                "target_stack_regions": dict(_TARGETS),
                actions.STATE_KEY: {"base": {"ref": "v0.9.0"}},
            },
        )
        with pytest.raises(RuntimeError, match="base release changed"):
            actions.action_upgrade_preflight(ctx)


# ─── prepare ─────────────────────────────────────────────────────────


def _assembly(tag: str = _RUN_ID, names: Any = None) -> dict[str, dict[str, Any]]:
    return {
        name: {"environment": "", "tags": {"GcoLiveValidationRun": tag}}
        for name in (names if names is not None else _TARGETS)
    }


class TestPrepare:
    def test_the_prepare_commands(self, tmp_path: Path) -> None:
        workspace = Workspace(tmp_path / "ws")
        commands = actions._prepare_commands(workspace)
        assert [label for label, _argv, _cwd in commands] == [
            "prepare venv",
            "prepare pip",
            "prepare npm",
            "prepare synth",
        ]
        pip = commands[1][1]
        assert pip[-4:] == [
            "--constraint",
            str(workspace.clone / "requirements-lock.txt"),
            "--editable",
            f"{workspace.clone}[cdk]",
        ]
        assert commands[3][1] == [str(workspace.gco), "stacks", "synth"]

    def test_a_prepared_workspace_is_reused(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        ctx = _ctx(
            _settings(tmp_path),
            state={
                "target_stack_regions": dict(_TARGETS),
                actions.STATE_KEY: {"workspace": {"prepared_at": "t"}},
            },
        )
        monkeypatch.setattr(
            actions, "_require_prepared_workspace", lambda ctx: {"prepared_at": "t"}
        )
        monkeypatch.setattr(actions, "reset_workspace", lambda *a: pytest.fail("must not rebuild"))
        assert actions.action_prepare(ctx) == {"reused": True, "prepared_at": "t"}

    @pytest.fixture
    def preparing(
        self, tmp_path: Path, repo: Repo, monkeypatch: pytest.MonkeyPatch
    ) -> tuple[Any, list[str]]:
        ctx = _ctx(_settings(tmp_path, repo))
        steps: list[str] = []

        def run_step(_ctx: Any, label: str, argv: Any, **_kwargs: Any) -> CommandResult:
            steps.append(label)
            return _result()

        monkeypatch.setattr(actions, "_run_step", run_step)
        monkeypatch.setattr(actions, "read_cloud_assembly", lambda clone: _assembly())
        monkeypatch.setattr(
            actions, "_expected_ecr_images", lambda ctx, names, root: [{"root": str(root)}]
        )
        return ctx, steps

    def test_prepare_builds_the_workspace_and_records_the_base_images(
        self, preparing: tuple[Any, list[str]], repo: Repo
    ) -> None:
        ctx, steps = preparing
        details = actions.action_prepare(ctx)
        workspace = Workspace(ctx.settings.workspace_dir)
        assert steps == ["prepare venv", "prepare pip", "prepare npm", "prepare synth"]
        assert details["clone"] == {"path": str(workspace.clone), "head": repo.base}
        assert details["synthetic_tag"] == "v1.0.1"
        assert details["stacks"] == sorted(_TARGETS)
        assert details["prior_release_ecr_images"] == [{"root": str(workspace.clone)}]
        state = ctx.checkpoint.state
        assert state["prior_release_ecr_images"] == [{"root": str(workspace.clone)}]
        record = state[actions.STATE_KEY]["workspace"]
        assert record["prepared_at"] and record["cdk_json"]["sha256"]
        context = json.loads((workspace.clone / "cdk.json").read_text(encoding="utf-8"))["context"]
        assert context["tags"]["GcoLiveValidationRun"] == _RUN_ID
        assert context[PROVIDER_LOG_CONTEXT] == "true"
        # The prepared workspace now passes the check deploy and upgrade repeat.
        workspace.gco.parent.mkdir(parents=True)
        workspace.gco.write_text("", encoding="utf-8")
        assert actions._require_prepared_workspace(ctx) is record

    def test_a_failed_step_stops_prepare_with_the_steps_so_far(
        self, preparing: tuple[Any, list[str]], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        ctx, _steps = preparing
        monkeypatch.setattr(
            actions,
            "_run_step",
            lambda _ctx, label, *_a, **_k: _result(0 if label == "prepare venv" else 1),
        )
        with pytest.raises(ActionFailure, match="failed at 'prepare pip'") as raised:
            actions.action_prepare(ctx)
        assert [step["step"] for step in raised.value.details["steps"]] == [
            "prepare venv",
            "prepare pip",
        ]

    @pytest.mark.parametrize(
        ("assembly", "match"),
        [
            (_assembly(names=_CONTROL), "a stack renamed or added between the releases"),
            (_assembly(tag="other"), "without the run tag"),
        ],
    )
    def test_the_base_app_must_deploy_the_targets_under_the_run_tag(
        self,
        preparing: tuple[Any, list[str]],
        monkeypatch: pytest.MonkeyPatch,
        assembly: dict[str, Any],
        match: str,
    ) -> None:
        ctx, _steps = preparing
        monkeypatch.setattr(actions, "read_cloud_assembly", lambda clone: assembly)
        with pytest.raises(ActionFailure, match=match):
            actions.action_prepare(ctx)
        assert "workspace" not in ctx.checkpoint.state.get(actions.STATE_KEY, {})

    def test_preparing_must_leave_only_cdk_json_changed(
        self, preparing: tuple[Any, list[str]], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        ctx, _steps = preparing
        monkeypatch.setattr(actions, "tracked_changes", lambda workspace: ["VERSION", "cdk.json"])
        with pytest.raises(
            ActionFailure, match=re.escape("gco upgrade refuses: VERSION, cdk.json")
        ):
            actions.action_prepare(ctx)


# ─── deploy, sentinels, upgrade ──────────────────────────────────────


@pytest.fixture
def settled(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Record the phase bookkeeping instead of calling AWS."""
    calls: list[str] = []
    monkeypatch.setattr(
        actions,
        "_adopt_run_tagged_stacks",
        lambda ctx, **kwargs: (
            calls.append(f"adopt:{kwargs['phase']}:{','.join(kwargs['replaceable'])}")
            or {"phase": kwargs["phase"]}
        ),
    )
    monkeypatch.setattr(
        actions, "_reconcile_stack_ownership", lambda ctx: calls.append("reconcile")
    )
    monkeypatch.setattr(
        actions, "_checkpoint_retained_kms_keys", lambda ctx: calls.append("kms") or []
    )
    monkeypatch.setattr(
        actions, "prune_local_cdk_asset_images_safely", lambda: {"removed_images": []}
    )
    monkeypatch.setattr(
        actions, "_require_prepared_workspace", lambda ctx: {"synthetic_tag": "v1.0.1"}
    )
    return calls


class TestBaseDeploy:
    def test_a_baseline_is_required(self, tmp_path: Path) -> None:
        ctx = _ctx(_settings(tmp_path))
        ctx.checkpoint.baseline = None
        with pytest.raises(RuntimeError, match="baseline is required"):
            actions.action_base_deploy(ctx)

    def test_the_base_gco_deploys_and_its_stacks_are_adopted(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, settled: list[str]
    ) -> None:
        ctx = _ctx(_settings(tmp_path))
        commands: list[list[str]] = []

        def run_step(_ctx: Any, label: str, argv: list[str], **kwargs: Any) -> CommandResult:
            assert ctx.checkpoint.deployment_attempted is True
            commands.append(argv)
            assert kwargs["timeout_seconds"] == ctx.settings.deploy_timeout_seconds
            assert kwargs["phase"] == "deploy"
            return _result()

        monkeypatch.setattr(actions, "_run_step", run_step)
        monkeypatch.setattr(actions, "_require_healthy_targets", lambda ctx: {"stacks": "healthy"})
        details = actions.action_base_deploy(ctx)
        workspace = Workspace(ctx.settings.workspace_dir)
        # No --tag: the run tag must reach the stacks through cdk.json alone.
        assert commands == [[str(workspace.gco), "stacks", "deploy-all", "--yes"]]
        assert settled == ["adopt:deploy:", "reconcile", "kms"]
        assert details["stacks"] == {"stacks": "healthy"}
        assert details["phase"]["command"]["exit_code"] == 0 and details["phase"]["finished_at"]
        assert details["phase"]["image_mirror"] == {"repositories": []}

    @staticmethod
    def _image_target(repository: str, *, kind: str = "configured-mirror") -> dict[str, Any]:
        return {
            "region": _REGION,
            "repository": repository,
            "tag": "v1.15.2",
            "sources": [{"kind": kind, "source_ref": f"docker.io/{repository}:v1.15.2"}],
        }

    def _mirroring_ctx(self, tmp_path: Path, *baseline_repositories: str) -> Any:
        ctx = _ctx(
            _settings(tmp_path),
            state={
                "target_stack_regions": dict(_TARGETS),
                "expected_ecr_images": [
                    self._image_target("gco/dockerhub/volcanosh/vc-scheduler"),
                    self._image_target("cdk-assets", kind="cdk-asset"),
                ],
                "prior_release_ecr_images": [
                    self._image_target("gco/dockerhub/volcanosh/vc-controller-manager")
                ],
            },
        )
        ctx.checkpoint.baseline = {
            "ecr_repositories": {_REGION: [{"name": name} for name in baseline_repositories]}
        }
        return ctx

    def test_the_mirror_may_only_copy_into_repositories_the_baseline_holds(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, settled: list[str]
    ) -> None:
        # Either release's mirror would create the controller repository from a
        # gco subprocess, with no ownership record; the asset repository is not
        # the mirror's and is ignored.
        ctx = self._mirroring_ctx(tmp_path, "gco/dockerhub/volcanosh/vc-scheduler")
        monkeypatch.setattr(actions, "_run_step", lambda *_a, **_k: pytest.fail("must not deploy"))
        with pytest.raises(
            RuntimeError,
            match=re.escape(f"records: {_REGION}:gco/dockerhub/volcanosh/vc-controller-manager."),
        ):
            actions.action_base_deploy(ctx)
        assert ctx.checkpoint.deployment_attempted is False
        assert ctx.checkpoint.state[actions.STATE_KEY]["phases"] == {}
        assert settled == []

    def test_the_mirror_repositories_are_recorded_on_the_phase(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, settled: list[str]
    ) -> None:
        ctx = self._mirroring_ctx(
            tmp_path,
            "gco/dockerhub/volcanosh/vc-controller-manager",
            "gco/dockerhub/volcanosh/vc-scheduler",
        )
        monkeypatch.setattr(actions, "_run_step", lambda *_a, **_k: _result())
        monkeypatch.setattr(actions, "_require_healthy_targets", lambda ctx: {})
        details = actions.action_base_deploy(ctx)
        assert details["phase"]["image_mirror"] == {
            "repositories": [
                f"{_REGION}:gco/dockerhub/volcanosh/vc-controller-manager",
                f"{_REGION}:gco/dockerhub/volcanosh/vc-scheduler",
            ]
        }

    def test_a_failed_deploy_still_adopts_what_it_left(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, settled: list[str]
    ) -> None:
        ctx = _ctx(_settings(tmp_path))
        monkeypatch.setattr(actions, "_run_step", lambda *_a, **_k: _result(1))
        with pytest.raises(ActionFailure, match="deploy-all exited with 1"):
            actions.action_base_deploy(ctx)
        assert settled[0] == "adopt:deploy:"

    def test_a_crashed_command_still_adopts_what_it_left(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, settled: list[str]
    ) -> None:
        ctx = _ctx(_settings(tmp_path))

        def explode(*_args: Any, **_kwargs: Any) -> CommandResult:
            raise OSError("no such file")

        monkeypatch.setattr(actions, "_run_step", explode)
        with pytest.raises(OSError, match="no such file"):
            actions.action_base_deploy(ctx)
        assert settled[0] == "adopt:deploy:"
        assert ctx.checkpoint.state[actions.STATE_KEY]["phases"]["deploy"]["finished_at"]

    def test_a_resumed_deploy_is_never_rerun(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, settled: list[str]
    ) -> None:
        phase = {
            "started_at": "t",
            "window_started_at": "w",
            "finished_at": "f",
            "command": {"exit_code": 0},
        }
        ctx = _ctx(
            _settings(tmp_path),
            state={
                "target_stack_regions": dict(_TARGETS),
                actions.STATE_KEY: {"phases": {"deploy": phase}},
            },
        )
        monkeypatch.setattr(actions, "_run_step", lambda *_a, **_k: pytest.fail("must not rerun"))
        monkeypatch.setattr(actions, "_require_healthy_targets", lambda ctx: {})
        assert actions.action_base_deploy(ctx)["phase"] == phase
        assert settled == ["reconcile"]
        phase["interrupted"] = True
        with pytest.raises(ActionFailure, match="was interrupted"):
            actions.action_base_deploy(ctx)


class TestSentinelsAction:
    def test_the_written_template_is_checkpointed(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        ctx = _ctx(_settings(tmp_path))
        monkeypatch.setattr(actions, "create_sentinel", lambda ctx, timeout_seconds: _stored())
        assert actions.action_sentinels(ctx) == {"template": _stored()}
        assert ctx.checkpoint.state[actions.STATE_KEY]["sentinel"] == _stored()


def _plan_document(**plan: Any) -> str:
    document = {
        "status": "ok",
        "up_to_date": False,
        "plan": {
            "target": "v1.0.1",
            "already_at_target": False,
            "install": {"cli_editable_from_checkout": True},
            "control_plane_stacks": _CONTROL,
            "workload_stacks": _WORKLOAD,
            **plan,
        },
    }
    return "streamed\n" + json.dumps(document, indent=2) + "\n"


class TestValidatedPlan:
    def test_a_plan_for_this_upgrade_is_accepted(self, tmp_path: Path) -> None:
        ctx = _ctx(_settings(tmp_path))
        assert actions._validated_plan(ctx, _result(stdout=_plan_document()), "v1.0.1") == {
            "target": "v1.0.1",
            "control_plane_stacks": _CONTROL,
            "workload_stacks": _WORKLOAD,
        }

    @pytest.mark.parametrize(
        ("result", "match"),
        [
            (_result(1, stdout=_plan_document()), "did not return a plan"),
            (_result(stdout="no document"), "did not return a plan"),
            (_result(stdout=_plan_document(already_at_target=True)), "already at the target"),
            (_result(stdout=_plan_document(target="v9.9.9")), "the target is 'v9.9.9', not v1.0.1"),
            (
                _result(stdout=_plan_document(install={"cli_editable_from_checkout": False})),
                "not the editable install",
            ),
            (_result(stdout=_plan_document(workload_stacks=[])), "do not partition the targets"),
            (
                _result(stdout=_plan_document(control_plane_stacks=[*_CONTROL, _WORKLOAD[0]])),
                "do not partition the targets",
            ),
            (_result(stdout=_plan_document(control_plane_stacks=_CONTROL[:1])), "do not partition"),
        ],
    )
    def test_a_plan_for_anything_else_is_refused(
        self, tmp_path: Path, result: CommandResult, match: str
    ) -> None:
        with pytest.raises(ActionFailure, match=match):
            actions._validated_plan(_ctx(_settings(tmp_path)), result, "v1.0.1")


class TestUpgradeAction:
    @pytest.fixture
    def upgrading(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, settled: list[str]
    ) -> tuple[Any, list[list[str]], list[str]]:
        ctx = _ctx(
            _settings(tmp_path),
            state={"target_stack_regions": dict(_TARGETS), "owned_stacks": _owned(_BEFORE)},
        )
        commands: list[list[str]] = []
        outputs = [
            _result(stdout=_plan_document()),
            _result(stdout="cdk output\n" + json.dumps({"status": "ok", "steps": {}}, indent=2)),
        ]

        def run_step(_ctx: Any, label: str, argv: list[str], **_kwargs: Any) -> CommandResult:
            commands.append(argv)
            return outputs.pop(0)

        monkeypatch.setattr(actions, "_run_step", run_step)
        monkeypatch.setattr(
            actions,
            "tag_candidate",
            lambda workspace, tag, candidate_sha: {"tag": tag, "commit": candidate_sha},
        )
        return ctx, commands, settled

    def test_the_base_gco_upgrades_to_the_candidate(
        self, upgrading: tuple[Any, list[list[str]], list[str]]
    ) -> None:
        ctx, commands, settled = upgrading
        details = actions.action_upgrade(ctx)
        gco = str(Workspace(ctx.settings.workspace_dir).gco)
        assert commands == [
            [gco, "-o", "json", "upgrade", "--check", "--ref", "v1.0.1"],
            [gco, "-o", "json", "upgrade", "--yes", "--ref", "v1.0.1", "--skip-container"],
        ]
        # The base generation's retained resources are checkpointed first.
        assert settled == [
            "reconcile",
            "kms",
            f"adopt:upgrade:{','.join(_WORKLOAD)}",
            "reconcile",
            "kms",
        ]
        phase = details["phase"]
        assert phase["before"] == dict(sorted(_BEFORE.items()))
        assert phase["plan"]["workload_stacks"] == _WORKLOAD
        assert phase["tag"] == {"tag": "v1.0.1", "commit": "a" * 40}
        assert phase["document"] == {"status": "ok", "steps": {}}

    def test_an_upgrade_that_does_not_report_ok_fails(
        self, upgrading: tuple[Any, list[list[str]], list[str]], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        ctx, _commands, _settled = upgrading
        outputs = [_result(stdout=_plan_document()), _result(stdout="no document")]
        monkeypatch.setattr(actions, "_run_step", lambda *_a, **_k: outputs.pop(0))
        with pytest.raises(ActionFailure, match="reported None, not 'ok'"):
            actions.action_upgrade(ctx)

    def test_a_resumed_upgrade_is_never_rerun(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, settled: list[str]
    ) -> None:
        phase = {"finished_at": "f", "command": {"exit_code": 0}, "document": {"status": "ok"}}
        ctx = _ctx(
            _settings(tmp_path),
            state={
                "target_stack_regions": dict(_TARGETS),
                actions.STATE_KEY: {"phases": {"upgrade": phase}},
            },
        )
        monkeypatch.setattr(actions, "_run_step", lambda *_a, **_k: pytest.fail("must not rerun"))
        assert actions.action_upgrade(ctx) == {"phase": phase}
        assert settled == ["reconcile"]


# ─── verify-upgrade ──────────────────────────────────────────────────


def _upgraded_state(tmp_path: Path) -> dict[str, Any]:
    owned = _owned(
        {
            name: (_BEFORE[name] if name in _CONTROL else f"arn:stack/{name}/new")
            for name in _TARGETS
        },
        **{name: {"replaced_generations": [{"stack_id": _BEFORE[name]}]} for name in _WORKLOAD},
    )
    return {
        "target_stack_regions": dict(_TARGETS),
        "owned_stacks": owned,
        actions.STATE_KEY: {
            "workspace": {"cdk_json": {"sha256": "sha"}},
            "sentinel": _stored(),
            "phases": {
                "upgrade": {
                    "finished_at": "f",
                    "before": dict(_BEFORE),
                    "plan": {
                        "target": "v1.0.1",
                        "control_plane_stacks": _CONTROL,
                        "workload_stacks": _WORKLOAD,
                    },
                    "document": {
                        "status": "ok",
                        "plan": {"target": "v1.0.1"},
                        "steps": {
                            "checkout": {"checked_out": "v1.0.1"},
                            "python": {"status": "ok"},
                            "stacks": {"ok": True, "destroyed": list(reversed(_WORKLOAD))},
                        },
                    },
                }
            },
        },
    }


class TestVerifyUpgrade:
    @pytest.fixture
    def verifying(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> tuple[Any, dict[str, Any]]:
        ctx = _ctx(_settings(tmp_path), state=_upgraded_state(tmp_path))
        observed: dict[str, Any] = {"head": "a" * 40, "changes": ["cdk.json"], "sha": "sha"}
        monkeypatch.setattr(actions, "checkout_head", lambda workspace: observed["head"])
        monkeypatch.setattr(actions, "tracked_changes", lambda workspace: observed["changes"])
        monkeypatch.setattr(actions, "sha256_file", lambda path: observed["sha"])
        monkeypatch.setattr(
            actions, "verify_sentinel", lambda ctx, written, timeout_seconds: written
        )
        monkeypatch.setattr(
            actions, "delete_sentinel", lambda ctx, timeout_seconds: {"deleted": True}
        )
        return ctx, observed

    def test_an_upgrade_that_kept_its_promises_passes(self, verifying: Any) -> None:
        ctx, _observed = verifying
        evidence = actions.action_verify_upgrade(ctx)
        assert evidence["checkout"] == {
            "head": "a" * 40,
            "tracked_changes": ["cdk.json"],
            "cdk_json_sha256": "sha",
        }
        assert evidence["stacks"]["before"] == _BEFORE
        assert evidence["stacks"]["after"][_WORKLOAD[1]] == f"arn:stack/{_WORKLOAD[1]}/new"
        assert evidence["sentinel"] == _stored()
        assert evidence["sentinel_cleanup"] == {"deleted": True}
        assert evidence["upgrade"]["steps"]["python"] == {"status": "ok"}

    def test_a_checkout_identical_to_the_commit_also_passes(self, verifying: Any) -> None:
        ctx, observed = verifying
        observed["changes"] = []
        actions.action_verify_upgrade(ctx)

    def test_the_upgrade_must_have_run(self, tmp_path: Path) -> None:
        state = _upgraded_state(tmp_path)
        del state[actions.STATE_KEY]["phases"]["upgrade"]["finished_at"]
        with pytest.raises(RuntimeError, match="upgrade action has not run"):
            actions.action_verify_upgrade(_ctx(_settings(tmp_path), state=state))
        state[actions.STATE_KEY]["phases"] = {}
        with pytest.raises(RuntimeError, match="upgrade action has not run"):
            actions.action_verify_upgrade(_ctx(_settings(tmp_path), state=state))

    def test_every_broken_promise_is_reported_together(
        self, verifying: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        ctx, observed = verifying
        observed.update(head="b" * 40, changes=["VERSION", "cdk.json"], sha="other")
        state = ctx.checkpoint.state
        state["owned_stacks"]["us-east-2"][_CONTROL[0]]["stack_id"] = "arn:replaced"
        state["owned_stacks"][_REGION][_WORKLOAD[0]]["stack_id"] = _BEFORE[_WORKLOAD[0]]
        state["owned_stacks"][_REGION][_WORKLOAD[1]]["replaced_generations"] = []
        document = state[actions.STATE_KEY]["phases"]["upgrade"]["document"]
        document["plan"]["target"] = "v9.9.9"
        document["steps"] = {
            "checkout": {},
            "python": {"status": "skipped"},
            "stacks": {"ok": False},
        }

        def failing_sentinel(*_args: Any, **_kwargs: Any) -> Any:
            raise RuntimeError("Template gone")

        def failing_cleanup(*_args: Any, **_kwargs: Any) -> Any:
            raise APIRequestError(500, "boom")

        monkeypatch.setattr(actions, "verify_sentinel", failing_sentinel)
        monkeypatch.setattr(actions, "delete_sentinel", failing_cleanup)
        with pytest.raises(ActionFailure) as raised:
            actions.action_verify_upgrade(ctx)
        message = str(raised.value)
        for fragment in (
            f"the base checkout is at {'b' * 40}",
            "changed tracked files: VERSION, cdk.json",
            "did not preserve cdk.json byte for byte",
            f"{_CONTROL[0]} was replaced",
            f"{_WORKLOAD[0]} was not recreated",
            f"{_WORKLOAD[1]} was not recreated",
            "names another target",
            "did not check out the target",
            "did not refresh the editable install",
            "did not destroy exactly the workload tier",
            "sentinel: Template gone",
            "sentinel cleanup: API request failed: boom",
        ):
            assert fragment in message, fragment
        assert raised.value.details["checkout"]["head"] == "b" * 40


# ─── The CLI entry point ─────────────────────────────────────────────


def _main_module() -> Any:
    import scripts.upgrade_validation.__main__ as main_module

    return main_module


def _argv(monkeypatch: pytest.MonkeyPatch, *args: str) -> None:
    monkeypatch.setattr(sys, "argv", ["python -m scripts.upgrade_validation", *args])


def _identity_args(repo: Repo, tmp_path: Path) -> tuple[str, ...]:
    return (
        "--repo-root",
        str(repo.root),
        "--expected-account",
        _ACCOUNT,
        "--expected-sha",
        repo.head.upper(),
        "--expected-branch",
        " feature ",
        "--report-dir",
        str(tmp_path / "reports" / "run-7"),
        "--run-id",
        "run-7",
    )


class TestMain:
    def test_list_actions_prints_the_registry(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        _argv(monkeypatch, "--list-actions")
        assert _main_module().main() == 0
        lines = capsys.readouterr().out.strip().splitlines()
        assert [line.split()[0] for line in lines] == list(build_action_registry())
        assert "[depends: none]" in lines[0]
        assert "[depends: baseline, prepare]" in next(
            line for line in lines if line.startswith("deploy")
        )

    def test_the_local_execution_guard_stops_everything(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        main_module = _main_module()

        def guard() -> None:
            raise RuntimeError("must not run in GitHub Actions")

        monkeypatch.setattr(main_module, "require_local_execution", guard)
        _argv(monkeypatch, "--expected-account", _ACCOUNT)
        assert main_module.main() == 1
        assert "could not start: must not run in GitHub Actions" in capsys.readouterr().err

    def test_the_base_defaults_to_the_newest_earlier_release(self, repo: Repo) -> None:
        main_module = _main_module()
        parser = main_module._build_parser()
        _git(repo.root, "tag", "v0.9.0", repo.base)
        _git(repo.root, "tag", "v1.0.1", repo.head)
        _git(repo.root, "tag", "not-a-release", repo.base)
        assert main_module._resolve_base(parser, repo.root, None, repo.head) == (
            "v1.0.0",
            repo.base,
        )
        assert main_module._resolve_base(parser, repo.root, "v0.9.0", repo.head) == (
            "v0.9.0",
            repo.base,
        )

    @pytest.mark.parametrize(
        ("requested", "match"),
        [("main", "must name a release tag"), ("v7.0.0", "is not a tag in")],
    )
    def test_a_named_base_must_be_an_existing_release_tag(
        self, repo: Repo, capsys: pytest.CaptureFixture[str], requested: str, match: str
    ) -> None:
        main_module = _main_module()
        with pytest.raises(SystemExit):
            main_module._resolve_base(main_module._build_parser(), repo.root, requested, repo.head)
        assert match in capsys.readouterr().err

    def test_a_checkout_without_an_earlier_release_needs_a_base(
        self, repo: Repo, capsys: pytest.CaptureFixture[str]
    ) -> None:
        main_module = _main_module()
        _git(repo.root, "tag", "-d", "v1.0.0")
        _git(repo.root, "tag", "v1.0.1", repo.head)
        with pytest.raises(SystemExit):
            main_module._resolve_base(main_module._build_parser(), repo.root, None, repo.head)
        assert "No release tag precedes" in capsys.readouterr().err

    @pytest.mark.parametrize(
        ("overrides", "match"),
        [
            ({"--expected-account": "12"}, "--expected-account must be an exact 12-digit"),
            ({"--expected-sha": "abc"}, "--expected-sha must be an exact 40-character"),
            ({"--expected-branch": " "}, "--expected-branch is required"),
            ({"--run-id": "../x"}, "--run-id must be 1-80 safe filename characters"),
            ({"--min-free-disk-gib": "-1"}, "--min-free-disk-gib must be >= 0"),
        ],
    )
    def test_malformed_identity_is_a_usage_error(
        self,
        tmp_path: Path,
        repo: Repo,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
        overrides: dict[str, str],
        match: str,
    ) -> None:
        main_module = _main_module()
        values = dict(
            zip(
                _identity_args(repo, tmp_path)[::2],
                _identity_args(repo, tmp_path)[1::2],
                strict=True,
            )
        )
        values.update(overrides)
        args = [item for pair in values.items() for item in pair]
        parser = main_module._build_parser()
        with pytest.raises(SystemExit):
            main_module._settings_from_args(parser, parser.parse_args(args))
        assert match in capsys.readouterr().err

    def test_settings_derive_the_report_and_workspace_paths(
        self, tmp_path: Path, repo: Repo
    ) -> None:
        main_module = _main_module()
        parser = main_module._build_parser()
        args = parser.parse_args([*_identity_args(repo, tmp_path), "--protected-stack", "Extra"])
        settings = main_module._settings_from_args(parser, args)
        assert settings.expected_sha == repo.head and settings.expected_branch == "feature"
        assert settings.base_ref == "v1.0.0" and settings.base_commit == repo.base
        assert settings.report_dir == tmp_path / "reports" / "run-7"
        assert settings.workspace_dir == tmp_path / "reports" / "run-7.workspace"
        assert settings.checkpoint_path == tmp_path / "reports" / "run-7" / "checkpoint.json"
        assert settings.protected_stack_names == ("CDKToolkit", "GCOGitHubOIDCStack", "Extra")
        assert settings.min_free_disk_gib == 20

    def test_the_default_run_id_and_report_directory(
        self, tmp_path: Path, repo: Repo, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        main_module = _main_module()
        monkeypatch.setattr(main_module, "DEFAULT_REPORTS_DIR", tmp_path / "home-reports")
        parser = main_module._build_parser()
        args = parser.parse_args(
            [
                "--repo-root",
                str(repo.root),
                "--expected-account",
                _ACCOUNT,
                "--expected-sha",
                repo.head,
                "--expected-branch",
                "feature",
            ]
        )
        settings = main_module._settings_from_args(parser, args)
        assert settings.run_id.endswith(f"-{repo.head[:12]}")
        assert settings.report_dir == tmp_path / "home-reports" / settings.run_id

    @pytest.fixture
    def runner_calls(self, monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
        main_module = _main_module()
        calls: dict[str, Any] = {"destroyed": False, "code": 0}

        class FakeRunner:
            def __init__(self, settings: Any, registry: Any = None) -> None:
                calls["settings"] = settings
                calls["registry"] = registry
                self.report = SimpleNamespace(title="", report_stem="")
                self.checkpoint = SimpleNamespace(destroyed=calls["destroyed"])
                calls["runner"] = self

            def run(self) -> int:
                return int(calls["code"])

        monkeypatch.setattr(main_module, "require_local_execution", lambda: None)
        monkeypatch.setattr(main_module, "LiveValidationRunner", FakeRunner)
        return calls

    def test_a_finished_run_removes_its_workspace(
        self,
        tmp_path: Path,
        repo: Repo,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
        runner_calls: dict[str, Any],
    ) -> None:
        runner_calls.update(destroyed=True, code=3)
        workspace = Workspace(tmp_path / "reports" / "run-7.workspace")
        workspace_module.reset_workspace(workspace, "run-7")
        _argv(monkeypatch, *_identity_args(repo, tmp_path))
        assert _main_module().main() == 3
        runner = runner_calls["runner"]
        assert runner.report.title == "GCO Upgrade Validation"
        assert runner.report.report_stem == "upgrade-validation"
        assert list(runner_calls["registry"]) == list(build_action_registry())
        assert not workspace.root.exists()
        assert f"Removed the workspace {workspace.root}" in capsys.readouterr().out
        # Nothing to remove the second time.
        _argv(monkeypatch, *_identity_args(repo, tmp_path))
        assert _main_module().main() == 3
        assert "Removed" not in capsys.readouterr().out

    def test_an_unfinished_run_keeps_its_workspace(
        self,
        tmp_path: Path,
        repo: Repo,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
        runner_calls: dict[str, Any],
    ) -> None:
        workspace = Workspace(tmp_path / "reports" / "run-7.workspace")
        workspace_module.reset_workspace(workspace, "run-7")
        _argv(monkeypatch, *_identity_args(repo, tmp_path))
        assert _main_module().main() == 0
        assert workspace.root.exists()
        assert "Workspace kept for resume or inspection" in capsys.readouterr().out

    def test_a_workspace_that_cannot_be_removed_is_reported(
        self,
        tmp_path: Path,
        repo: Repo,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
        runner_calls: dict[str, Any],
    ) -> None:
        runner_calls["destroyed"] = True
        (tmp_path / "reports" / "run-7.workspace").mkdir(parents=True)
        _argv(monkeypatch, *_identity_args(repo, tmp_path))
        assert _main_module().main() == 0
        assert "Could not remove the workspace" in capsys.readouterr().err

    def test_an_interrupt_before_the_runner_is_exit_130(
        self,
        tmp_path: Path,
        repo: Repo,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        main_module = _main_module()

        def interrupted(*_args: Any, **_kwargs: Any) -> Any:
            raise KeyboardInterrupt

        monkeypatch.setattr(main_module, "require_local_execution", lambda: None)
        monkeypatch.setattr(main_module, "LiveValidationRunner", interrupted)
        _argv(monkeypatch, *_identity_args(repo, tmp_path))
        assert main_module.main() == 130
        assert "interrupted before the runner initialized" in capsys.readouterr().err

    def test_a_runner_startup_failure_writes_a_failed_report(
        self,
        tmp_path: Path,
        repo: Repo,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        main_module = _main_module()

        def exploding(*_args: Any, **_kwargs: Any) -> Any:
            raise RuntimeError("cdk context unreadable")

        monkeypatch.setattr(main_module, "require_local_execution", lambda: None)
        monkeypatch.setattr(main_module, "LiveValidationRunner", exploding)
        _argv(monkeypatch, *_identity_args(repo, tmp_path), "--actions", "preflight")
        assert main_module.main() == 1
        assert "could not start: RuntimeError: cdk context unreadable" in capsys.readouterr().err
        report_dir = tmp_path / "reports" / "run-7"
        payload = json.loads((report_dir / "upgrade-validation.json").read_text(encoding="utf-8"))
        assert payload["status"] == "failed" and payload["run_id"] == "run-7"
        assert payload["identity"]["base_ref"] == "v1.0.0"
        assert payload["selected_actions"] == ["preflight"]
        markdown = (report_dir / "upgrade-validation.md").read_text(encoding="utf-8")
        assert markdown.startswith("# GCO Upgrade Validation")

    def test_a_usage_error_writes_no_report(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        main_module = _main_module()
        monkeypatch.setattr(main_module, "require_local_execution", lambda: None)
        _argv(monkeypatch, "--expected-account", "12", "--report-dir", str(tmp_path / "report"))
        assert main_module.main() == 1
        assert "could not start: SystemExit: 2" in capsys.readouterr().err
        assert not (tmp_path / "report").exists()
