"""Path arguments the MCP tools hand to the ``gco`` CLI.

The CLI runners (``cli_runner._run_cli``, ``_run_cli_async`` and the
long-task runner) pass argv through unchanged. Each tool checks its own path
arguments before calling them:

* a local file or directory the CLI opens goes through
  ``cli_runner.reject_paths_outside_project_root``: resolved against the
  project root the CLI runs from, symlinks followed, and refused unless it
  stays inside that root;
* a name or ID the CLI puts into an API request path goes through
  ``cli_runner.reject_traversal_segments``, which refuses a ``..`` segment
  (the HTTP client drops dot segments before sending, so the request would
  reach another route).

Free text (prompts, chat messages, descriptions) is not checked. These tests
pin all three rules for the helpers and for every tool that has such an
argument.
"""

from __future__ import annotations

import asyncio
import contextlib
import importlib
import inspect
import json
import os
import sys
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "gco_mcp"))

import cli_runner
import run_mcp

# Flags for the gated tools exercised here (delete_job, delete_template,
# delete_webhook, images_build, deploy_stack, deploy_all).
_GATE_FLAGS = (
    "GCO_ENABLE_DESTRUCTIVE_OPERATIONS",
    "GCO_ENABLE_IMAGE_PUBLISH",
    "GCO_ENABLE_INFRASTRUCTURE_DEPLOY",
)


def _registered_tool_names() -> set[str]:
    return {tool.name for tool in asyncio.run(run_mcp.mcp._list_tools())}


@pytest.fixture(scope="module")
def tools() -> Iterator[Any]:
    """``run_mcp`` reloaded with the gates above for this module only.

    Every tool the reload adds to the shared FastMCP singleton is removed
    again afterwards, so registry-count tests elsewhere see the default set.
    """
    before = _registered_tool_names()
    with patch.dict(os.environ, dict.fromkeys(_GATE_FLAGS, "true")):
        importlib.reload(run_mcp)
        yield run_mcp
    for name in _registered_tool_names() - before:
        with contextlib.suppress(Exception):
            run_mcp.mcp.local_provider.remove_tool(name)
    importlib.reload(run_mcp)


@pytest.fixture
def project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A project root with one manifest, installed as ``cli_runner.PROJECT_ROOT``."""
    root = tmp_path / "project"
    (root / "jobs").mkdir(parents=True)
    (root / "jobs" / "job.yaml").write_text("kind: Job\n", encoding="utf-8")
    monkeypatch.setattr(cli_runner, "PROJECT_ROOT", root)
    return root


class _Recorder:
    """Stand-in for the CLI runners that records each argv it is given."""

    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    def sync(self, *args: str, **_kwargs: Any) -> str:
        self.calls.append(list(args))
        return '{"ok": true}'

    async def long_task(self, argv: list[str], **_kwargs: Any) -> str:
        self.calls.append(list(argv))
        return '{"status": "ok"}'


@pytest.fixture
def cli(monkeypatch: pytest.MonkeyPatch) -> _Recorder:
    recorder = _Recorder()
    monkeypatch.setattr(cli_runner, "_run_cli", recorder.sync)
    for module in ("tools.images", "tools.stacks"):
        monkeypatch.setattr(f"{module}._run_long_task", recorder.long_task)
    return recorder


def _call(fn: Callable[..., Any], **kwargs: Any) -> str:
    """Call a tool, sync or async, and return its JSON string."""
    if "ctx" in inspect.signature(fn).parameters:
        kwargs.setdefault("ctx", None)
        kwargs.setdefault("progress", None)
    result = fn(**kwargs)
    if inspect.iscoroutine(result):
        result = asyncio.run(result)
    assert isinstance(result, str)
    return result


# ---------------------------------------------------------------------------
# reject_paths_outside_project_root
# ---------------------------------------------------------------------------


class TestRejectPathsOutsideProjectRoot:
    @pytest.mark.parametrize(
        "value",
        [
            "jobs/job.yaml",
            "./jobs/job.yaml",
            "jobs/../jobs/job.yaml",
            "out/stack-outputs.json",  # need not exist yet (an outputs file)
            "~/job.yaml",  # not expanded: the CLI opens ./~/job.yaml
            ".",
            None,
            "",
        ],
    )
    def test_paths_inside_the_root_are_accepted(self, project: Path, value: str | None) -> None:
        assert cli_runner.reject_paths_outside_project_root(manifest_path=value) is None

    def test_absolute_path_inside_the_root_is_accepted(self, project: Path) -> None:
        inside = str(project / "jobs" / "job.yaml")
        assert cli_runner.reject_paths_outside_project_root(manifest_path=inside) is None

    @pytest.mark.parametrize(
        "make_value",
        [
            lambda root: "../outside.yaml",
            lambda root: "jobs/../../outside.yaml",
            lambda root: str(root.parent / "outside.yaml"),
            # A sibling whose name starts with the root's name is still outside.
            lambda root: str(root.parent / f"{root.name}-other" / "job.yaml"),
            lambda root: "/",
        ],
        ids=["dotdot", "nested-dotdot", "absolute", "prefix-sibling", "filesystem-root"],
    )
    def test_paths_outside_the_root_are_refused(
        self, project: Path, make_value: Callable[[Path], str]
    ) -> None:
        value = make_value(project)
        result = cli_runner.reject_paths_outside_project_root(manifest_path=value)
        assert result is not None
        payload = json.loads(result)
        assert payload["code"] == "path_outside_project_root"
        assert payload["parameter"] == "manifest_path"
        assert value in payload["error"]

    def test_symlink_out_of_the_root_is_refused(self, project: Path, tmp_path: Path) -> None:
        secrets = tmp_path / "secrets"
        secrets.mkdir()
        (secrets / "credentials").write_text("token\n", encoding="utf-8")
        (project / "escape").symlink_to(secrets, target_is_directory=True)
        (project / "inner").symlink_to(project / "jobs", target_is_directory=True)

        refused = cli_runner.reject_paths_outside_project_root(context="escape/credentials")
        assert refused is not None
        assert json.loads(refused)["parameter"] == "context"
        assert cli_runner.reject_paths_outside_project_root(context="inner/job.yaml") is None

    def test_root_given_through_a_symlink_still_confines(
        self, project: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        alias = tmp_path / "alias"
        alias.symlink_to(project, target_is_directory=True)
        monkeypatch.setattr(cli_runner, "PROJECT_ROOT", alias)

        for value in ("jobs/job.yaml", str(project / "jobs/job.yaml"), str(alias / "jobs")):
            assert cli_runner.reject_paths_outside_project_root(manifest_path=value) is None
        outside = str(tmp_path / "outside.yaml")
        assert cli_runner.reject_paths_outside_project_root(manifest_path=outside) is not None

    def test_nul_byte_is_refused(self, project: Path) -> None:
        result = cli_runner.reject_paths_outside_project_root(script_path="jobs/a\x00b")
        assert result is not None
        assert json.loads(result)["code"] == "path_outside_project_root"

    def test_the_offending_parameter_is_named(self, project: Path) -> None:
        result = cli_runner.reject_paths_outside_project_root(
            manifest_path="jobs/job.yaml", outputs_file="../out.json"
        )
        assert result is not None
        assert json.loads(result)["parameter"] == "outputs_file"


# ---------------------------------------------------------------------------
# reject_traversal_segments
# ---------------------------------------------------------------------------


class TestRejectTraversalSegments:
    @pytest.mark.parametrize("value", ["..", "../x", "a/../b", "x/..", "a\\..\\b", "/v1/../.."])
    def test_dot_dot_segments_are_refused(self, value: str) -> None:
        result = cli_runner.reject_traversal_segments(job_name=value)
        assert result is not None
        payload = json.loads(result)
        assert payload == {
            "error": f"job_name must not contain a '..' path segment: {value}",
            "code": "path_traversal_segment",
            "parameter": "job_name",
        }

    @pytest.mark.parametrize(
        "value", ["train-1", "a..b", "...", ".", "v1.2.3", "/v1/chat/completions", None, ""]
    )
    def test_other_values_are_accepted(self, value: str | None) -> None:
        assert cli_runner.reject_traversal_segments(job_name=value) is None

    def test_the_offending_parameter_is_named(self) -> None:
        result = cli_runner.reject_traversal_segments(job_name="ok", namespace="..")
        assert result is not None
        assert json.loads(result)["parameter"] == "namespace"


# ---------------------------------------------------------------------------
# Tools with local path arguments
# ---------------------------------------------------------------------------

# (tool, path parameter, other required arguments)
_LOCAL_PATH_TOOLS: list[tuple[str, str, dict[str, Any]]] = [
    ("submit_job_sqs", "manifest_path", {"region": "us-east-1"}),
    ("submit_job_api", "manifest_path", {}),
    ("check_job_policy", "manifest_path", {}),
    ("queue_submit", "manifest_path", {"region": "us-east-1"}),
    ("templates_create", "manifest_path", {"name": "train"}),
    ("dag_validate", "manifest_path", {}),
    ("dag_run", "manifest_path", {"region": "us-east-1"}),
    ("slurm_submit", "script_path", {"region": "us-east-1"}),
    ("images_build", "context", {"name": "trainer"}),
    ("deploy_stack", "outputs_file", {"stack_name": "gco-us-east-1"}),
    ("deploy_all", "outputs_file", {}),
]


@pytest.mark.parametrize(
    ("tool", "parameter", "extra"), _LOCAL_PATH_TOOLS, ids=[t[0] for t in _LOCAL_PATH_TOOLS]
)
class TestLocalPathTools:
    def test_path_outside_the_project_is_refused_before_the_cli_runs(
        self,
        tools: Any,
        project: Path,
        cli: _Recorder,
        tool: str,
        parameter: str,
        extra: dict[str, Any],
    ) -> None:
        outside = str(project.parent / "elsewhere" / "file.yaml")
        payload = json.loads(_call(getattr(tools, tool), **{parameter: outside}, **extra))
        assert payload["code"] == "path_outside_project_root"
        assert payload["parameter"] == parameter
        assert cli.calls == []

    def test_path_inside_the_project_reaches_the_cli_unchanged(
        self,
        tools: Any,
        project: Path,
        cli: _Recorder,
        tool: str,
        parameter: str,
        extra: dict[str, Any],
    ) -> None:
        inside = "jobs/../jobs/job.yaml"
        _call(getattr(tools, tool), **{parameter: inside}, **extra)
        assert len(cli.calls) == 1
        assert inside in cli.calls[0]


# ---------------------------------------------------------------------------
# Tools whose names or IDs land in an API request path
# ---------------------------------------------------------------------------

_JOB = {"job_name": "train-1", "region": "us-east-1", "namespace": "gco-jobs"}

# (tool, parameter checked, arguments with a clean value for that parameter)
_REQUEST_PATH_TOOLS: list[tuple[str, str, dict[str, Any]]] = [
    ("get_job", "job_name", _JOB),
    ("get_job", "namespace", _JOB),
    ("get_job_logs", "job_name", _JOB),
    ("get_job_events", "job_name", _JOB),
    ("get_job_pods", "job_name", _JOB),
    ("get_job_metrics", "job_name", _JOB),
    ("retry_job", "job_name", _JOB),
    ("retry_job", "namespace", _JOB),
    ("delete_job", "job_name", _JOB),
    ("get_pod_logs", "job_name", {**_JOB, "pod_name": "train-1-abc"}),
    ("get_pod_logs", "pod_name", {**_JOB, "pod_name": "train-1-abc"}),
    ("templates_get", "name", {"name": "train"}),
    ("delete_template", "name", {"name": "train"}),
    ("templates_run", "template_name", {"template_name": "train", "job_name": "j", "region": "r"}),
    ("delete_webhook", "name", {"name": "hook-1"}),
    ("queue_get", "job_id", {"job_id": "abc123"}),
    ("invoke_inference", "api_path", {"name": "llm", "prompt": "hi", "api_path": "/v1/chat"}),
]


@pytest.mark.parametrize(
    ("tool", "parameter", "arguments"),
    _REQUEST_PATH_TOOLS,
    ids=[f"{t[0]}-{t[1]}" for t in _REQUEST_PATH_TOOLS],
)
class TestRequestPathTools:
    def test_dot_dot_segment_is_refused_before_the_cli_runs(
        self, tools: Any, cli: _Recorder, tool: str, parameter: str, arguments: dict[str, Any]
    ) -> None:
        bad = {**arguments, parameter: "../../templates/other"}
        payload = json.loads(_call(getattr(tools, tool), **bad))
        assert payload["code"] == "path_traversal_segment"
        assert payload["parameter"] == parameter
        assert cli.calls == []

    def test_clean_value_reaches_the_cli(
        self, tools: Any, cli: _Recorder, tool: str, parameter: str, arguments: dict[str, Any]
    ) -> None:
        _call(getattr(tools, tool), **arguments)
        assert len(cli.calls) == 1
        assert arguments[parameter] in cli.calls[0]


# ---------------------------------------------------------------------------
# Free text is not checked
# ---------------------------------------------------------------------------


class TestFreeTextIsPassedThrough:
    def test_prompt_mentioning_a_parent_directory(self, tools: Any, cli: _Recorder) -> None:
        prompt = "Compare src/../lib layouts"
        _call(tools.invoke_inference, name="llm", prompt=prompt)
        assert prompt in cli.calls[0]

    def test_chat_message_with_dot_segments(self, tools: Any, cli: _Recorder) -> None:
        messages = [{"role": "user", "content": "why is a/../b == b?"}]
        _call(tools.chat_inference, name="llm", messages=messages)
        body = json.loads(cli.calls[0][cli.calls[0].index("-d") + 1])
        assert body["messages"] == messages

    def test_template_description(self, tools: Any, project: Path, cli: _Recorder) -> None:
        description = "copied from ../shared/train.yaml"
        _call(
            tools.templates_create,
            name="train",
            manifest_path="jobs/job.yaml",
            description=description,
        )
        assert description in cli.calls[0]

    def test_slurm_working_directory_on_the_cluster(
        self, tools: Any, project: Path, cli: _Recorder
    ) -> None:
        _call(
            tools.slurm_submit,
            region="us-east-1",
            script_path="jobs/job.yaml",
            working_directory="/fsx/runs/../shared",
        )
        assert "/fsx/runs/../shared" in cli.calls[0]


def test_runners_pass_dot_segments_to_the_cli() -> None:
    """The async runner, like the sync one, no longer filters argv itself."""
    process = AsyncMock()
    process.communicate.return_value = (b'{"ok": true}', b"")
    process.returncode = 0
    with patch(
        "cli_runner.asyncio.create_subprocess_exec", AsyncMock(return_value=process)
    ) as spawn:
        result = asyncio.run(cli_runner._run_cli_async("inference", "invoke", "m", "-p", "a/../b"))
    assert json.loads(result) == {"ok": True}
    assert spawn.call_args.args[-1] == "a/../b"
