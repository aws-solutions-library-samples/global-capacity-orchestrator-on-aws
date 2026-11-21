"""User-supplied names and IDs in GCO API request paths (PATH-ENC).

Several commands put a name or an ID into the path of an API request
(``/api/v1/templates/{name}``, ``/api/v1/webhooks/{id}``,
``/api/v1/queue/jobs/{id}``, ``/api/v1/jobs/{namespace}/{name}/...``) and
``gco inference invoke --path`` appends its value to the endpoint's path. The
HTTP client removes dot segments before it sends a request, and
``quote(..., safe="")`` leaves ``.`` and ``..`` alone, so before this change
``gco templates delete ../webhooks/ID`` sent ``DELETE /api/v1/webhooks/ID``.

Every such value now goes through ``cli.aws_client.api_path_segment`` (one
encoded segment; empty, ``.``/``..`` and ``a/..`` refused) and ``--path``
through ``checked_api_sub_path``. A refused value never reaches the network.
"""

from __future__ import annotations

import re
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
import requests
from click.testing import CliRunner

from cli.aws_client import (
    GCOAWSClient,
    api_path_segment,
    checked_api_sub_path,
)
from cli.config import GCOConfig
from cli.main import cli

_BASE = "https://api.example.com/prod"

_SEGMENT_REFUSAL = "cannot be used as a name or ID in an API request path"
_SUB_PATH_REFUSAL = "must start with '/' and have no '.' or '..' segment"

_TRAVERSALS = ["..", ".", "", "../webhooks/abc", "a/..", "a/./b", "..\\webhooks", "x/../../y"]


# ---------------------------------------------------------------------------
# The helpers
# ---------------------------------------------------------------------------


def test_the_http_client_removes_dot_segments_quote_leaves_intact() -> None:
    """The premise: an unchecked ``..`` reaches a different route."""
    prepared = requests.Request("DELETE", f"{_BASE}/api/v1/templates/../webhooks/abc").prepare()
    assert prepared.path_url == "/prod/api/v1/webhooks/abc"


@pytest.mark.parametrize(
    ("value", "encoded"),
    [
        ("train-job-1", "train-job-1"),
        ("a b", "a%20b"),
        ("a/b", "a%2Fb"),
        ("x?y#z", "x%3Fy%23z"),
        ("%2e%2e", "%252e%252e"),
        ("..a", "..a"),
        ("a..", "a.."),
        (42, "42"),
    ],
)
def test_api_path_segment_encodes_one_literal_segment(value: Any, encoded: str) -> None:
    assert api_path_segment(value) == encoded
    # One segment on the wire, whatever the value held.
    prepared = requests.Request("GET", f"{_BASE}/api/v1/templates/{encoded}").prepare()
    assert prepared.path_url == f"/prod/api/v1/templates/{encoded}"


@pytest.mark.parametrize("value", _TRAVERSALS)
def test_api_path_segment_refuses_dot_and_empty_segments(value: str) -> None:
    with pytest.raises(ValueError, match=_SEGMENT_REFUSAL):
        api_path_segment(value)


@pytest.mark.parametrize(
    "path",
    ["/v1/completions", "/generate", "/v1/models?cursor=..", "/v1/x#..", "/v1/..a/b.."],
)
def test_checked_api_sub_path_accepts_paths_under_the_prefix(path: str) -> None:
    assert checked_api_sub_path(path) == path


@pytest.mark.parametrize(
    "path",
    [
        "v1/completions",
        "2/v1/completions",
        "/../../api/v1/templates",
        "/v1/./completions",
        "/v1/..",
        "/%2e%2e/api/v1/templates",
        "/%2E%2E/%2e/x",
        "/v1\\..\\x",
        "",
    ],
)
def test_checked_api_sub_path_refuses_paths_that_leave_the_prefix(path: str) -> None:
    with pytest.raises(ValueError, match=re.escape(_SUB_PATH_REFUSAL)):
        checked_api_sub_path(path)


# ---------------------------------------------------------------------------
# GCOAWSClient job routes
# ---------------------------------------------------------------------------


@pytest.fixture
def client() -> GCOAWSClient:
    with patch("cli.aws_client.boto3.Session"):
        aws = GCOAWSClient(GCOConfig())
    response = MagicMock(ok=True, status_code=200)
    response.json.return_value = {"logs": "", "pods": []}
    aws.make_authenticated_request = MagicMock(return_value=response)  # type: ignore[method-assign]
    return aws


def _job_calls(aws: GCOAWSClient, namespace: str, name: str) -> list[Any]:
    return [
        lambda: aws.get_job_details(name, namespace, "us-east-1"),
        lambda: aws.get_job_logs(name, namespace, "us-east-1"),
        lambda: aws.delete_job(name, namespace, "us-east-1"),
        lambda: aws.get_job_events(name, namespace, "us-east-1"),
        lambda: aws.get_job_pods(name, namespace, "us-east-1"),
        lambda: aws.get_pod_logs(name, "pod-0", namespace, "us-east-1"),
        lambda: aws.get_job_metrics(name, namespace, "us-east-1"),
        lambda: aws.retry_job(name, namespace, "us-east-1"),
    ]


def test_every_job_route_encodes_namespace_and_name(client: GCOAWSClient) -> None:
    for call in _job_calls(client, "team a", "job?1"):
        call()
    paths = [entry.kwargs["path"] for entry in client.make_authenticated_request.call_args_list]  # type: ignore[attr-defined]
    assert paths == [
        "/api/v1/jobs/team%20a/job%3F1",
        "/api/v1/jobs/team%20a/job%3F1/logs?tail=100",
        "/api/v1/jobs/team%20a/job%3F1",
        "/api/v1/jobs/team%20a/job%3F1/events",
        "/api/v1/jobs/team%20a/job%3F1/pods",
        "/api/v1/jobs/team%20a/job%3F1/pods/pod-0/logs?tail=100",
        "/api/v1/jobs/team%20a/job%3F1/metrics",
        "/api/v1/jobs/team%20a/job%3F1/retry",
    ]


@pytest.mark.parametrize(("namespace", "name"), [("gco-jobs", "../../webhooks/x"), ("..", "j")])
def test_job_routes_refuse_traversal_before_any_request(
    client: GCOAWSClient, namespace: str, name: str
) -> None:
    for call in _job_calls(client, namespace, name):
        with pytest.raises(ValueError, match=_SEGMENT_REFUSAL):
            call()
    client.make_authenticated_request.assert_not_called()  # type: ignore[attr-defined]


def test_pod_logs_encode_the_pod_name_and_container(client: GCOAWSClient) -> None:
    client.get_pod_logs("job", "pod/x", "gco-jobs", "us-east-1", container="main&tail=1")
    path = client.make_authenticated_request.call_args.kwargs["path"]  # type: ignore[attr-defined]
    assert path == "/api/v1/jobs/gco-jobs/job/pods/pod%2Fx/logs?tail=100&container=main%26tail%3D1"
    with pytest.raises(ValueError, match=_SEGMENT_REFUSAL):
        client.get_pod_logs("job", "..", "gco-jobs", "us-east-1")


# ---------------------------------------------------------------------------
# Commands that build the path themselves
# ---------------------------------------------------------------------------


def _invoke(args: list[str]) -> tuple[Any, MagicMock]:
    aws = MagicMock()
    aws.call_api.return_value = {"template": {}, "job": {}, "success": True}
    with patch("cli.aws_client.get_aws_client", return_value=aws):
        result = CliRunner().invoke(cli, args)
    return result, aws


_COMMANDS = [
    (["templates", "get", "{v}"], "Failed to get template"),
    (["templates", "delete", "{v}", "-y"], "Failed to delete template"),
    (["templates", "run", "{v}", "--name", "j", "-r", "us-east-1"], "Failed to run template"),
    (["webhooks", "delete", "{v}", "-y"], "Failed to delete webhook"),
    (["queue", "get", "{v}"], "Failed to get job"),
    (["queue", "cancel", "{v}", "-y"], "Failed to cancel job"),
]


@pytest.mark.parametrize(("template", "message"), _COMMANDS)
@pytest.mark.parametrize("value", ["..", "../webhooks/abc", "a/.."])
def test_commands_refuse_a_traversal_value_without_calling_the_api(
    template: list[str], message: str, value: str
) -> None:
    result, aws = _invoke([part.replace("{v}", value) for part in template])

    assert result.exit_code == 1
    assert message in result.output
    assert "cannot be used as a name or ID" in result.output
    aws.call_api.assert_not_called()


@pytest.mark.parametrize(
    ("args", "path"),
    [
        (["templates", "get", "a b"], "/api/v1/templates/a%20b"),
        (["templates", "delete", "x/y", "-y"], "/api/v1/templates/x%2Fy"),
        (
            ["templates", "run", "t#1", "--name", "j", "-r", "us-east-1"],
            "/api/v1/jobs/from-template/t%231",
        ),
        (["webhooks", "delete", "id?x", "-y"], "/api/v1/webhooks/id%3Fx"),
        (["queue", "get", "q 1"], "/api/v1/queue/jobs/q%201"),
        (["queue", "cancel", "q/1", "-y"], "/api/v1/queue/jobs/q%2F1"),
    ],
)
def test_commands_send_the_value_as_one_encoded_segment(args: list[str], path: str) -> None:
    result, aws = _invoke(args)

    assert result.exit_code == 0, result.output
    assert aws.call_api.call_args.kwargs["path"] == path


def test_slurm_job_paths_go_through_the_encoder() -> None:
    from cli.commands.slurm_cmd import _job_path

    assert _job_path(42) == "/api/v1/slurm/jobs/42"


# ---------------------------------------------------------------------------
# gco inference invoke --path
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("api_path", ["/../../api/v1/templates", "v1/completions", "/%2e%2e/x"])
def test_inference_invoke_refuses_a_path_that_leaves_the_endpoint(api_path: str) -> None:
    manager = MagicMock()
    aws = MagicMock()
    with (
        patch("cli.inference.get_inference_manager", return_value=manager),
        patch("cli.aws_client.get_aws_client", return_value=aws),
    ):
        result = CliRunner().invoke(
            cli, ["inference", "invoke", "my-llm", "-p", "hi", "--path", api_path]
        )

    assert result.exit_code == 1
    assert "--path:" in result.output
    assert _SUB_PATH_REFUSAL in result.output
    # Refused before the endpoint lookup, so nothing reaches AWS.
    manager.get_endpoint.assert_not_called()
    aws.make_authenticated_request.assert_not_called()
