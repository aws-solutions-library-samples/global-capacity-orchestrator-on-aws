"""Regression tests for operational CLI copy, retry, and tunnel behavior."""

from __future__ import annotations

import subprocess
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, Mock, call, patch

import pytest

from cli import files, ssm_tunnel
from cli.aws_client import ApiEndpoint, GCOAWSClient


def _file_system_client() -> files.FileSystemClient:
    client = object.__new__(files.FileSystemClient)
    client.config = SimpleNamespace(project_name="gco")
    return client


def test_pod_copy_cannot_report_success_without_creating_destination(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    destination = tmp_path / "nested" / "download.bin"
    commands: list[list[str]] = []

    monkeypatch.setattr(files, "update_kubeconfig", lambda *_args: None)

    def run(command: list[str], **_kwargs: object) -> SimpleNamespace:
        commands.append(command)
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(subprocess, "run", run)

    with pytest.raises(RuntimeError, match="reported success but did not create"):
        _file_system_client().download_from_pod(
            region="us-east-1",
            pod_name="trainer-0",
            remote_path="/outputs/model.bin",
            local_path=str(destination),
        )

    assert destination.parent.is_dir()
    assert not destination.exists()
    assert commands == [
        [
            "kubectl",
            "cp",
            "--",
            "gco-jobs/trainer-0:/outputs/model.bin",
            str(destination),
        ]
    ]


def test_storage_copy_cannot_report_success_without_creating_destination_and_cleans_up(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    destination = tmp_path / "nested" / "checkpoint"
    commands: list[list[str]] = []

    monkeypatch.setattr(files, "update_kubeconfig", lambda *_args: None)

    def run(command: list[str], **_kwargs: object) -> SimpleNamespace:
        commands.append(command)
        stdout = "Running" if command[:3] == ["kubectl", "get", "pod"] else ""
        return SimpleNamespace(returncode=0, stdout=stdout, stderr="")

    monkeypatch.setattr(subprocess, "run", run)

    with pytest.raises(RuntimeError, match="reported success but did not create"):
        _file_system_client().download_from_storage(
            region="us-east-1",
            remote_path="runs/checkpoint",
            local_path=str(destination),
        )

    assert destination.parent.is_dir()
    assert not destination.exists()
    assert any(command[:2] == ["kubectl", "cp"] for command in commands)
    assert commands[-1][:3] == ["kubectl", "delete", "pod"]


def test_post_connect_tunnel_exit_uses_one_diagnostic_read(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    proc = SimpleNamespace()
    connection = MagicMock()
    diagnostic = Mock(side_effect=[None, "exit code 17; session closed"])

    monkeypatch.setattr(ssm_tunnel.subprocess, "Popen", lambda *_args, **_kwargs: proc)
    monkeypatch.setattr(
        ssm_tunnel.socket,
        "create_connection",
        lambda *_args, **_kwargs: connection,
    )
    monkeypatch.setattr(ssm_tunnel, "exited_api_tunnel_detail", diagnostic)
    monkeypatch.setattr(ssm_tunnel, "stop_api_tunnel", lambda *_args, **_kwargs: (b"", b""))

    with pytest.raises(
        RuntimeError,
        match="exited during readiness: exit code 17; session closed",
    ):
        ssm_tunnel.start_api_tunnel(
            "i-0123456789abcdef0",
            "https://ABC.gr7.us-east-1.eks.amazonaws.com",
            8443,
            "us-east-1",
            ready_wait_seconds=0,
        )

    connection.close.assert_called_once_with()
    assert diagnostic.call_args_list == [call(proc), call(proc)]


def test_retry_closes_transient_response_before_backoff_when_credentials_disappear() -> None:
    events: list[str] = []

    with (
        patch("cli.aws_client.get_config") as get_config,
        patch("cli.aws_client.boto3.Session") as session_factory,
        patch("cli.aws_client.requests.request") as request,
        patch("cli.aws_client.SigV4Auth"),
        patch("cli.aws_client.time.sleep", side_effect=lambda _seconds: events.append("sleep")),
    ):
        get_config.return_value = SimpleNamespace(cache_ttl_seconds=300)
        session_factory.return_value.get_credentials.side_effect = [MagicMock(), None]
        response = MagicMock(status_code=503)
        response.close.side_effect = lambda: events.append("close")
        request.return_value = response

        client = GCOAWSClient()
        client._api_endpoint_cache = ApiEndpoint(
            url="https://api.example.com/prod",
            region="us-east-1",
            api_id="test",
        )
        client._cache_timestamp = time.time()

        result = client.make_authenticated_request(
            "GET",
            "/api/v1/health",
            max_attempts=2,
        )

    assert result is response
    assert events == ["close", "sleep"]
    response.close.assert_called_once_with()
    request.assert_called_once()
