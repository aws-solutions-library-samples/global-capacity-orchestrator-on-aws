"""CLI tests for human-friendly S3 bucket discovery and storage sync."""

from __future__ import annotations

import signal
import threading
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

from cli.commands.storage_cmd import (
    _cooperative_storage_sigterm,
    _StorageSyncTerminated,
)
from cli.config import GCOConfig
from cli.main import cli


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


@pytest.fixture
def config() -> GCOConfig:
    return GCOConfig(output_format="table")


@pytest.fixture(autouse=True)
def use_config(config: GCOConfig):
    with patch("cli.main.get_config", return_value=config):
        yield


def _download_result(**overrides: Any) -> dict[str, Any]:
    result: dict[str, Any] = {
        "alias": "cluster-shared",
        "bucket": "bucket",
        "region": "us-east-2",
        "direction": "download",
        "source": "s3://bucket/prefix/",
        "destination": "/tmp/data",
        "prefix": "prefix/",
        "dry_run": False,
        "force": False,
        "objects_scanned": 2,
        "directory_markers": 0,
        "files_planned": 1,
        "files_downloaded": 1,
        "files_skipped": 1,
        "bytes_planned": 5,
        "bytes_downloaded": 5,
    }
    result.update(overrides)
    return result


def _upload_result(**overrides: Any) -> dict[str, Any]:
    result: dict[str, Any] = {
        "alias": "cluster-shared",
        "bucket": "bucket",
        "region": "us-east-2",
        "direction": "upload",
        "source": "/tmp/data",
        "destination": "s3://bucket/prefix/",
        "prefix": "prefix/",
        "dry_run": False,
        "force": False,
        "files_scanned": 2,
        "objects_scanned": 2,
        "objects_probed": 2,
        "files_planned": 2,
        "files_uploaded": 2,
        "files_skipped": 0,
        "bytes_planned": 10,
        "bytes_uploaded": 10,
    }
    result.update(overrides)
    return result


class TestStorageListCommand:
    def test_list_table(self, runner: CliRunner) -> None:
        manager = MagicMock()
        manager.list_buckets.return_value = [
            {
                "alias": "cluster-shared",
                "scope": "global",
                "region": "us-east-2",
                "bucket": "physical",
                "purpose": "Shared data",
                "s3_uri": "s3://physical/",
            }
        ]
        with patch("cli.storage.get_storage_manager", return_value=manager):
            result = runner.invoke(cli, ["storage", "list", "--region", "us-east-1"])
        assert result.exit_code == 0
        assert "cluster-shared" in result.output
        assert "physical" in result.output
        manager.list_buckets.assert_called_once_with(region="us-east-1")

    def test_list_empty_table(self, runner: CliRunner) -> None:
        manager = MagicMock()
        manager.list_buckets.return_value = []
        with patch("cli.storage.get_storage_manager", return_value=manager):
            result = runner.invoke(cli, ["storage", "list"])
        assert result.exit_code == 0
        assert "No user-facing GCO S3 buckets" in result.output

    def test_list_empty_json(self, runner: CliRunner) -> None:
        manager = MagicMock()
        manager.list_buckets.return_value = []
        with patch("cli.storage.get_storage_manager", return_value=manager):
            result = runner.invoke(cli, ["--output", "json", "storage", "list"])
        assert result.exit_code == 0
        assert result.output.strip() == "[]"

    def test_list_error(self, runner: CliRunner) -> None:
        manager = MagicMock()
        manager.list_buckets.side_effect = RuntimeError("denied")
        with patch("cli.storage.get_storage_manager", return_value=manager):
            result = runner.invoke(cli, ["storage", "list"])
        assert result.exit_code == 1
        assert "Failed to discover" in result.output
        assert "denied" in result.output


class TestStorageSyncCommand:
    def test_default_download_table_and_hidden_contract(self, runner: CliRunner) -> None:
        manager = MagicMock()
        manager.sync.return_value = _download_result()
        argv = [
            "storage",
            "sync",
            "--region",
            "us-east-1",
            "--prefix",
            "prefix",
            "--force",
            "--_gco-storage-root",
            "/safe",
            "--_gco-storage-root-device",
            "12",
            "--_gco-storage-root-inode",
            "34",
            "regional-shared",
            "data",
        ]
        with patch("cli.storage.get_storage_manager", return_value=manager):
            result = runner.invoke(cli, argv)
        assert result.exit_code == 0
        assert "Downloading" in result.output
        assert "Downloaded 1 file(s) (5 bytes)" in result.output
        assert "Skipped 1 current file" in result.output
        assert "Source: s3://bucket/prefix/" in result.output
        assert "Destination: /tmp/data" in result.output
        manager.sync.assert_called_once_with(
            "regional-shared",
            "data",
            region="us-east-1",
            prefix="prefix",
            direction="download",
            dry_run=False,
            force=True,
            confinement_root="/safe",
            confinement_device=12,
            confinement_inode=34,
        )

    def test_upload_table(self, runner: CliRunner) -> None:
        manager = MagicMock()
        manager.sync.return_value = _upload_result()
        with patch("cli.storage.get_storage_manager", return_value=manager):
            result = runner.invoke(
                cli,
                [
                    "storage",
                    "sync",
                    "cluster-shared",
                    "data",
                    "--direction",
                    "UPLOAD",
                    "--prefix",
                    "out",
                ],
            )
        assert result.exit_code == 0
        assert "Uploading" in result.output
        assert "Uploaded 2 file(s) (10 bytes)" in result.output
        assert "Destination: s3://bucket/prefix/" in result.output
        assert manager.sync.call_args.kwargs["direction"] == "upload"

    @pytest.mark.parametrize(
        ("direction", "result_value", "expected"),
        [
            (
                "download",
                _download_result(
                    dry_run=True,
                    files_downloaded=0,
                    files_planned=3,
                    bytes_planned=30,
                ),
                "would be downloaded",
            ),
            (
                "upload",
                _upload_result(
                    dry_run=True,
                    files_uploaded=0,
                    files_planned=3,
                    bytes_planned=30,
                ),
                "would be uploaded",
            ),
        ],
    )
    def test_dry_run_table(
        self,
        runner: CliRunner,
        direction: str,
        result_value: dict[str, Any],
        expected: str,
    ) -> None:
        manager = MagicMock()
        manager.sync.return_value = result_value
        with patch("cli.storage.get_storage_manager", return_value=manager):
            result = runner.invoke(
                cli,
                [
                    "storage",
                    "sync",
                    "cluster-shared",
                    "data",
                    "--direction",
                    direction,
                    "--dry-run",
                ],
            )
        assert result.exit_code == 0
        assert f"Planning {direction}" in result.output
        assert "3 file(s) (30 bytes)" in result.output
        assert expected in result.output

    def test_sync_json_prints_structured_result(self, runner: CliRunner) -> None:
        manager = MagicMock()
        manager.sync.return_value = _upload_result()
        with patch("cli.storage.get_storage_manager", return_value=manager):
            result = runner.invoke(
                cli,
                [
                    "--output",
                    "json",
                    "storage",
                    "sync",
                    "cluster-shared",
                    "data",
                    "--direction",
                    "upload",
                ],
            )
        assert result.exit_code == 0
        assert '"direction": "upload"' in result.output
        assert "Uploading" not in result.output

    def test_sync_error(self, runner: CliRunner) -> None:
        manager = MagicMock()
        manager.sync.side_effect = RuntimeError("transfer failed")
        with patch("cli.storage.get_storage_manager", return_value=manager):
            result = runner.invoke(cli, ["storage", "sync", "cluster-shared", "data"])
        assert result.exit_code == 1
        assert "Failed to sync GCO S3 bucket" in result.output
        assert "transfer failed" in result.output

    def test_invalid_direction_is_click_error(self, runner: CliRunner) -> None:
        result = runner.invoke(
            cli,
            [
                "storage",
                "sync",
                "cluster-shared",
                "data",
                "--direction",
                "both",
            ],
        )
        assert result.exit_code == 2
        assert "Invalid value for '--direction'" in result.output


class TestCooperativeSigterm:
    def test_main_thread_turns_sigterm_into_exception_and_restores_handler(self) -> None:
        previous = object()
        with (
            patch("cli.commands.storage_cmd.signal.getsignal", return_value=previous),
            patch("cli.commands.storage_cmd.signal.signal") as set_signal,
            pytest.raises(_StorageSyncTerminated, match="terminated"),
            _cooperative_storage_sigterm(),
        ):
            handler = set_signal.call_args_list[0].args[1]
            handler(signal.SIGTERM, None)
        assert set_signal.call_args_list[-1].args == (signal.SIGTERM, previous)

    def test_non_main_thread_does_not_install_handler(self) -> None:
        completed: list[bool] = []

        def run() -> None:
            with patch("cli.commands.storage_cmd.signal.signal") as set_signal:
                with _cooperative_storage_sigterm():
                    completed.append(True)
                set_signal.assert_not_called()

        thread = threading.Thread(target=run)
        thread.start()
        thread.join(timeout=5)
        assert not thread.is_alive()
        assert completed == [True]
