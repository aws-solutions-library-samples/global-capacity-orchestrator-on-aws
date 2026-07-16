"""MCP confinement and cancellation tests for local S3 storage sync."""

from __future__ import annotations

import asyncio
import importlib.util
import json
import os
import sys
from pathlib import Path
from types import ModuleType
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

MCP_ROOT = Path(__file__).resolve().parents[1] / "gco_mcp"
if str(MCP_ROOT) not in sys.path:
    sys.path.insert(0, str(MCP_ROOT))

import cli_runner  # noqa: E402
import local_data  # noqa: E402


class _FakeMCP:
    def tool(self, **_kwargs: Any):
        def decorate(function: Any) -> Any:
            return function

        return decorate


class _RunnerModule(ModuleType):
    def __init__(self) -> None:
        super().__init__("cli_runner")
        self.calls: list[tuple[str, ...]] = []
        self.call_kwargs: list[dict[str, Any]] = []
        self.staged_observations: list[dict[str, Any]] = []
        self.async_calls: list[tuple[tuple[str, ...], dict[str, Any]]] = []

    def _run_cli(self, *args: str, **kwargs: Any) -> str:
        self.calls.append(args)
        self.call_kwargs.append(kwargs)
        for argument in args:
            if not argument.startswith("/dev/fd/"):
                continue
            metadata = os.stat(argument, follow_symlinks=False)
            self.staged_observations.append(
                {
                    "argument": argument,
                    "device": metadata.st_dev,
                    "inode": metadata.st_ino,
                    "mode": metadata.st_mode,
                    "pass_fds": kwargs.get("pass_fds"),
                }
            )
        return json.dumps({"args": args})

    async def _run_cli_async(self, *args: str, **kwargs: Any) -> str:
        self.async_calls.append((args, kwargs))
        return json.dumps({"args": args})


def _load_storage_tool(
    *,
    enabled: bool,
    upload_enabled: bool = False,
) -> tuple[ModuleType, _RunnerModule]:
    """Load the tool module against isolated decorators and a recording runner."""
    runner = _RunnerModule()
    audit = ModuleType("audit")
    audit.audit_logged = lambda function: function  # type: ignore[attr-defined]
    flags = ModuleType("feature_flags")
    flags.FLAG_LOCAL_STORAGE_SYNC = "GCO_ENABLE_LOCAL_STORAGE_SYNC"  # type: ignore[attr-defined]
    flags.FLAG_MODEL_UPLOAD = "GCO_ENABLE_MODEL_UPLOAD"  # type: ignore[attr-defined]
    flags.is_enabled = lambda flag: (  # type: ignore[attr-defined]
        upload_enabled if flag == flags.FLAG_MODEL_UPLOAD else enabled
    )
    server = ModuleType("server")
    server.mcp = _FakeMCP()  # type: ignore[attr-defined]
    name = f"_gco_mcp_storage_test_{id(runner)}"
    spec = importlib.util.spec_from_file_location(name, MCP_ROOT / "tools" / "storage.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    replacements = {
        "cli_runner": runner,
        "audit": audit,
        "feature_flags": flags,
        "server": server,
        name: module,
    }
    with patch.dict(sys.modules, replacements):
        spec.loader.exec_module(module)
    return module, runner


class TestMCPStorageTools:
    def test_read_only_storage_wrappers_build_literal_argv(self) -> None:
        module, runner = _load_storage_tool(enabled=False)

        assert json.loads(module.list_storage_contents("us-east-1"))["args"] == [
            "files",
            "ls",
            "-r",
            "us-east-1",
        ]
        module.list_storage_contents("us-west-2", "/outputs")
        module.list_file_systems()
        module.list_file_systems("eu-west-1")
        asyncio.run(module.list_storage_buckets())
        asyncio.run(module.list_storage_buckets("ap-south-1"))
        asyncio.run(module.files_get("us-east-1"))
        asyncio.run(module.files_get("us-east-1", "fsx"))
        asyncio.run(module.files_access_points())
        asyncio.run(module.files_access_points("eu-central-1"))

        assert ("files", "ls", "-r", "us-west-2", "/outputs") in runner.calls
        assert ("files", "list") in runner.calls
        assert ("files", "list", "-r", "eu-west-1") in runner.calls
        assert ("storage", "list") in runner.calls
        assert ("storage", "list", "--region", "ap-south-1") in runner.calls
        assert ("files", "get", "us-east-1", "-t", "fsx") in runner.calls
        assert ("files", "access-points", "-r", "eu-central-1") in runner.calls
        assert not hasattr(module, "upload_to_regional_bucket")
        assert not hasattr(module, "sync_storage_bucket")

    def test_regional_upload_is_gated_and_uses_private_descriptor_snapshot(
        self,
        tmp_path: Path,
    ) -> None:
        module, runner = _load_storage_tool(enabled=False, upload_enabled=True)
        root = tmp_path / "root"
        source = root / "model.bin"
        source.parent.mkdir()
        source.write_bytes(b"weights")
        source_identity = (source.stat().st_dev, source.stat().st_ino)

        with patch.dict(os.environ, {"GCO_STORAGE_LOCAL_ROOT": str(root)}):
            result = asyncio.run(
                module.upload_to_regional_bucket("./model.bin", "us-east-1", "models")
            )

        args = json.loads(result)["args"]
        assert args[0:2] == ["models", "upload-regional"]
        assert args[2].startswith("/dev/fd/")
        assert args[3:] == ["-r", "us-east-1", "--prefix", "models"]
        observation = runner.staged_observations[-1]
        assert (observation["device"], observation["inode"]) == source_identity
        assert observation["pass_fds"] == runner.call_kwargs[-1]["pass_fds"]
        assert len(observation["pass_fds"]) == 1
        assert not list(root.glob(".gco-mcp-upload-*"))
        assert not hasattr(module, "sync_storage_bucket")

    def test_regional_upload_rejects_descendant_symlink(self, tmp_path: Path) -> None:
        module, runner = _load_storage_tool(enabled=False, upload_enabled=True)
        root = tmp_path / "root"
        source = root / "dataset"
        source.mkdir(parents=True)
        outside = tmp_path / "outside.txt"
        outside.write_text("secret", encoding="utf-8")
        (source / "escape.txt").symlink_to(outside)

        with patch.dict(os.environ, {"GCO_STORAGE_LOCAL_ROOT": str(root)}):
            result = asyncio.run(
                module.upload_to_regional_bucket("dataset", "us-east-1", "uploads")
            )

        payload = json.loads(result)
        assert payload["code"] == "local_data_path_rejected"
        assert "symbolic link" in payload["error"]
        assert runner.calls == []
        assert not list(root.glob(".gco-mcp-upload-*"))

    def test_resolve_sync_local_path_contract(self, tmp_path: Path) -> None:
        module, _ = _load_storage_tool(enabled=True)
        root = tmp_path / "root"
        child = root / "child"
        child.mkdir(parents=True)

        with patch.dict(os.environ, {"GCO_STORAGE_LOCAL_ROOT": str(root)}):
            contract = module._resolve_sync_local_path("child", require_exists=True)
            root_contract = module._resolve_sync_local_path(str(root), require_exists=True)

        expected = root.stat()
        assert contract.local_argument == "child"
        assert contract.resolved_path == child.resolve()
        assert contract.root == root.resolve()
        assert (contract.device, contract.inode) == (expected.st_dev, expected.st_ino)
        assert root_contract.local_argument == "."

    def test_resolve_sync_local_path_rejections(self, tmp_path: Path) -> None:
        module, _ = _load_storage_tool(enabled=True)
        with patch.dict(os.environ, {}, clear=True), pytest.raises(ValueError, match="must be set"):
            module._resolve_sync_local_path("child", require_exists=False)

        root = tmp_path / "root"
        root.mkdir()
        outside = tmp_path / "outside"
        outside.mkdir()
        with patch.dict(os.environ, {"GCO_STORAGE_LOCAL_ROOT": str(root)}):
            with pytest.raises(ValueError, match="stay within"):
                module._resolve_sync_local_path(str(outside), require_exists=False)
            with pytest.raises(ValueError, match="does not exist"):
                module._resolve_sync_local_path("missing", require_exists=True)

            link = root / "link"
            link.symlink_to(outside, target_is_directory=True)
            with pytest.raises(ValueError, match="stay within"):
                module._resolve_sync_local_path("link/file", require_exists=False)

            with (
                patch.object(local_data.os, "name", "nt"),
                pytest.raises(ValueError, match="requires"),
            ):
                module._resolve_sync_local_path("child", require_exists=False)

        missing_root = tmp_path / "missing-root"
        with (
            patch.dict(os.environ, {"GCO_STORAGE_LOCAL_ROOT": str(missing_root)}),
            pytest.raises(FileNotFoundError),
        ):
            module._resolve_sync_local_path("child", require_exists=False)

    def test_sync_storage_bucket_builds_confined_argv(self, tmp_path: Path) -> None:
        module, runner = _load_storage_tool(enabled=True)
        root = tmp_path / "root"
        source = root / "--prefix"
        source.mkdir(parents=True)

        with patch.dict(os.environ, {"GCO_STORAGE_LOCAL_ROOT": str(root)}):
            result = asyncio.run(
                module.sync_storage_bucket(
                    "regional-shared:us-east-1",
                    "--prefix",
                    direction="UPLOAD",
                    region="us-east-1",
                    prefix="models",
                    dry_run=True,
                    force=True,
                )
            )

        assert json.loads(result)["args"][0:3] == ["storage", "sync", "--direction"]
        args, kwargs = runner.async_calls[-1]
        assert args[0:4] == ("storage", "sync", "--direction", "upload")
        assert "--_gco-storage-root" in args
        assert "--_gco-storage-root-device" in args
        assert "--_gco-storage-root-inode" in args
        assert args[args.index("--region") : args.index("--region") + 2] == (
            "--region",
            "us-east-1",
        )
        assert args[args.index("--prefix") : args.index("--prefix") + 2] == (
            "--prefix",
            "models",
        )
        assert "--dry-run" in args
        assert "--force" in args
        assert args[-3:] == ("--", "regional-shared:us-east-1", "--prefix")
        assert kwargs == {"timeout_seconds": 3600, "terminate_grace_seconds": 30}

    def test_sync_download_allows_missing_destination_and_omits_empty_options(
        self,
        tmp_path: Path,
    ) -> None:
        module, runner = _load_storage_tool(enabled=True)
        root = tmp_path / "root"
        root.mkdir()
        with patch.dict(os.environ, {"GCO_STORAGE_LOCAL_ROOT": str(root)}):
            asyncio.run(module.sync_storage_bucket("cluster-shared", "new-destination"))
        args, _ = runner.async_calls[-1]
        assert "--region" not in args
        assert "--prefix" not in args
        assert "--dry-run" not in args
        assert "--force" not in args
        assert args[-2:] == ("cluster-shared", "new-destination")

    def test_sync_returns_structured_path_rejection(self, tmp_path: Path) -> None:
        module, runner = _load_storage_tool(enabled=True)
        root = tmp_path / "root"
        root.mkdir()
        with patch.dict(os.environ, {"GCO_STORAGE_LOCAL_ROOT": str(root)}):
            outside = asyncio.run(
                module.sync_storage_bucket(
                    "cluster-shared",
                    str(tmp_path / "outside"),
                )
            )
            missing_upload = asyncio.run(
                module.sync_storage_bucket(
                    "cluster-shared",
                    "missing",
                    direction="upload",
                )
            )
        assert json.loads(outside)["code"] == "local_storage_path_rejected"
        assert json.loads(missing_upload)["code"] == "local_storage_path_rejected"
        assert runner.async_calls == []


class _FakeProcess:
    def __init__(
        self,
        *,
        stdout: bytes = b"",
        stderr: bytes = b"",
        final_returncode: int = 0,
        wait_for_signal: bool = False,
        terminate_releases: bool = True,
    ) -> None:
        self.stdout = stdout
        self.stderr = stderr
        self.final_returncode = final_returncode
        self.returncode: int | None = None if wait_for_signal else final_returncode
        self.wait_for_signal = wait_for_signal
        self.terminate_releases = terminate_releases
        self.released = asyncio.Event()
        self.terminated = False
        self.killed = False

    async def communicate(self) -> tuple[bytes, bytes]:
        if self.wait_for_signal:
            await self.released.wait()
        self.returncode = self.final_returncode
        return self.stdout, self.stderr

    def terminate(self) -> None:
        self.terminated = True
        if self.terminate_releases:
            self.released.set()

    def kill(self) -> None:
        self.killed = True
        self.released.set()


class TestAsyncCliRunner:
    def test_executable_resolution_prefers_current_environment(self, tmp_path: Path) -> None:
        executable = tmp_path / "bin" / "python"
        executable.parent.mkdir()
        executable.write_text("", encoding="utf-8")
        gco = executable.parent / "gco"
        gco.write_text("", encoding="utf-8")
        with patch.object(cli_runner.sys, "executable", str(executable)):
            assert cli_runner._gco_executable() == str(gco)

        gco.unlink()
        with (
            patch.object(cli_runner.sys, "executable", str(executable)),
            patch("cli_runner.shutil.which", return_value="/usr/local/bin/gco"),
        ):
            assert cli_runner._gco_executable() == "/usr/local/bin/gco"
        with (
            patch.object(cli_runner.sys, "executable", str(executable)),
            patch("cli_runner.shutil.which", return_value=None),
        ):
            assert cli_runner._gco_executable() == "gco"

    def test_sync_runner_rejects_traversal(self) -> None:
        result = cli_runner._run_cli("storage", "sync", "../outside")
        assert "path traversal" in json.loads(result)["error"]

    @pytest.mark.parametrize(
        ("process", "expected"),
        [
            (_FakeProcess(stdout=b'{"ok": true}'), {"ok": True}),
            (_FakeProcess(stdout=b""), {"status": "ok"}),
            (
                _FakeProcess(stderr=b"denied", final_returncode=2),
                {"error": "denied", "exit_code": 2},
            ),
            (
                _FakeProcess(stdout=b"fallback", final_returncode=3),
                {"error": "fallback", "exit_code": 3},
            ),
        ],
    )
    def test_async_runner_result_mapping(
        self,
        process: _FakeProcess,
        expected: dict[str, Any],
    ) -> None:
        async def run() -> str:
            with patch(
                "cli_runner.asyncio.create_subprocess_exec", AsyncMock(return_value=process)
            ):
                return await cli_runner._run_cli_async("storage", "list")

        assert json.loads(asyncio.run(run())) == expected

    def test_async_runner_rejects_traversal_and_missing_cli(self) -> None:
        assert (
            "path traversal"
            in json.loads(asyncio.run(cli_runner._run_cli_async("storage", "../outside")))["error"]
        )

        async def missing() -> str:
            with patch(
                "cli_runner.asyncio.create_subprocess_exec",
                AsyncMock(side_effect=FileNotFoundError),
            ):
                return await cli_runner._run_cli_async("storage", "list")

        assert "not found" in json.loads(asyncio.run(missing()))["error"].lower()

    def test_async_runner_timeout_terminates_and_drains(self) -> None:
        process = _FakeProcess(wait_for_signal=True)

        async def run() -> str:
            with patch(
                "cli_runner.asyncio.create_subprocess_exec", AsyncMock(return_value=process)
            ):
                return await cli_runner._run_cli_async(
                    "storage",
                    "list",
                    timeout_seconds=0,
                    terminate_grace_seconds=1,
                )

        result = asyncio.run(run())
        assert "timed out" in json.loads(result)["error"].lower()
        assert process.terminated
        assert not process.killed

    def test_async_runner_cancellation_terminates_and_reraises(self) -> None:
        process = _FakeProcess(wait_for_signal=True)

        async def run() -> None:
            with patch(
                "cli_runner.asyncio.create_subprocess_exec", AsyncMock(return_value=process)
            ):
                task = asyncio.create_task(cli_runner._run_cli_async("storage", "list"))
                await asyncio.sleep(0)
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await task

        asyncio.run(run())
        assert process.terminated

    def test_stop_process_escalates_to_kill_after_grace(self) -> None:
        process = _FakeProcess(wait_for_signal=True, terminate_releases=False)

        async def run() -> None:
            communication = asyncio.create_task(process.communicate())
            await cli_runner._stop_cli_process(process, communication, grace_seconds=0)

        asyncio.run(run())
        assert process.terminated
        assert process.killed

    def test_stop_process_handles_already_exited_process(self) -> None:
        process = _FakeProcess(stdout=b"done")

        async def run() -> None:
            communication = asyncio.create_task(process.communicate())
            await cli_runner._stop_cli_process(process, communication, grace_seconds=1)

        asyncio.run(run())
        assert not process.terminated
        assert not process.killed
