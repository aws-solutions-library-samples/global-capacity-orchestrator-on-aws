"""Focused contracts for the delete-only Helm teardown provider."""

from __future__ import annotations

import json
import os
from unittest.mock import MagicMock, patch

import pytest
from botocore.exceptions import ClientError

from tests._lambda_imports import load_lambda_module

teardown_provider = load_lambda_module("helm-installer", module_name="teardown_provider")

_STATE_MACHINE_ARN = "arn:aws:states:us-east-1:123456789012:stateMachine:gco-helm-teardown"
_INSTALL_STATE_MACHINE_ARN = "arn:aws:states:us-east-1:123456789012:stateMachine:gco-helm-install"


def _provider_env() -> dict[str, str]:
    return {
        "TEARDOWN_STATE_MACHINE_ARN": _STATE_MACHINE_ARN,
        "INSTALL_STATE_MACHINE_ARN": _INSTALL_STATE_MACHINE_ARN,
    }


def _event(request_type: str) -> dict:
    return {
        "RequestType": request_type,
        "StackId": "arn:aws:cloudformation:us-east-1:123456789012:stack/gco/abc",
        "RequestId": "request-123",
        "LogicalResourceId": "HelmTeardown",
        "PhysicalResourceId": "helm-teardown",
        "ResourceProperties": {
            "ClusterName": "gco-us-east-1",
            "Region": "us-east-1",
            "EnabledCharts": ["keda", "kueue"],
            "Charts": {"keda": {"values": {"watchNamespace": "gco-jobs"}}},
            "KedaOperatorRoleArn": "arn:aws:iam::123456789012:role/keda",
        },
    }


@pytest.mark.parametrize("request_type", ["Create", "Update"])
def test_create_and_update_are_noops(request_type):
    sfn = MagicMock()
    with patch.object(teardown_provider, "_sfn", return_value=sfn):
        result = teardown_provider.on_event(_event(request_type))

    assert result == {"PhysicalResourceId": "helm-teardown"}
    sfn.start_execution.assert_not_called()


def test_delete_starts_retry_stable_ordered_execution():
    event = _event("Delete")
    sfn = MagicMock()
    sfn.list_executions.return_value = {"executions": []}
    with (
        patch.dict(os.environ, _provider_env()),
        patch.object(teardown_provider, "_sfn", return_value=sfn),
    ):
        result = teardown_provider.on_event(event)

    assert result == {"PhysicalResourceId": "helm-teardown"}
    kwargs = sfn.start_execution.call_args.kwargs
    assert kwargs["stateMachineArn"] == _STATE_MACHINE_ARN
    assert kwargs["name"] == teardown_provider._execution_name(event)
    execution_input = json.loads(kwargs["input"])
    assert execution_input["EnabledCharts"] == ["keda", "kueue"]
    # Drain is unconditional because ListExecutions is eventually consistent.
    assert execution_input["WaitForInFlightSeconds"] == 16 * 60
    sfn.list_executions.assert_called_once_with(
        stateMachineArn=_INSTALL_STATE_MACHINE_ARN,
        statusFilter="RUNNING",
        maxResults=100,
    )


def test_delete_stops_and_drains_running_install_execution():
    running_arn = "arn:aws:states:us-east-1:123456789012:execution:gco-helm-install:running-1"
    sfn = MagicMock()
    sfn.list_executions.return_value = {
        "executions": [{"executionArn": running_arn}],
    }
    with (
        patch.dict(os.environ, _provider_env()),
        patch.object(teardown_provider, "_sfn", return_value=sfn),
    ):
        teardown_provider.on_event(_event("Delete"))

    sfn.stop_execution.assert_called_once_with(executionArn=running_arn)
    execution_input = json.loads(sfn.start_execution.call_args.kwargs["input"])
    assert execution_input["WaitForInFlightSeconds"] == 16 * 60


def test_terminal_stop_race_is_idempotent():
    running_arn = "arn:aws:states:us-east-1:123456789012:execution:gco-helm-install:raced"
    validation_error = ClientError(
        {"Error": {"Code": "ValidationException", "Message": "not running"}},
        "StopExecution",
    )
    sfn = MagicMock()
    sfn.list_executions.return_value = {
        "executions": [{"executionArn": running_arn}],
    }
    sfn.stop_execution.side_effect = validation_error
    sfn.describe_execution.return_value = {"status": "SUCCEEDED"}

    delay = teardown_provider._stop_running_install_executions(sfn, _INSTALL_STATE_MACHINE_ARN)

    assert delay == 16 * 60
    sfn.describe_execution.assert_called_once_with(executionArn=running_arn)


def test_duplicate_delete_event_is_idempotent():
    error = ClientError(
        {"Error": {"Code": "ExecutionAlreadyExists", "Message": "exists"}},
        "StartExecution",
    )
    sfn = MagicMock()
    sfn.list_executions.return_value = {"executions": []}
    sfn.start_execution.side_effect = error
    with (
        patch.dict(os.environ, _provider_env()),
        patch.object(teardown_provider, "_sfn", return_value=sfn),
    ):
        teardown_provider.on_event(_event("Delete"))


def test_is_complete_waits_then_succeeds():
    sfn = MagicMock()
    sfn.list_executions.return_value = {"executions": []}
    with (
        patch.dict(os.environ, _provider_env()),
        patch.object(teardown_provider, "_sfn", return_value=sfn),
    ):
        sfn.describe_execution.return_value = {"status": "RUNNING"}
        assert teardown_provider.is_complete(_event("Delete")) == {"IsComplete": False}
        sfn.describe_execution.return_value = {"status": "SUCCEEDED"}
        assert teardown_provider.is_complete(_event("Delete")) == {"IsComplete": True}

    assert sfn.list_executions.call_count == 2


def test_is_complete_stops_execution_missed_by_initial_snapshot():
    running_arn = "arn:aws:states:us-east-1:123456789012:execution:gco-helm-install:late"
    sfn = MagicMock()
    sfn.list_executions.return_value = {
        "executions": [{"executionArn": running_arn}],
    }
    sfn.describe_execution.return_value = {"status": "RUNNING"}

    with (
        patch.dict(os.environ, _provider_env()),
        patch.object(teardown_provider, "_sfn", return_value=sfn),
    ):
        assert teardown_provider.is_complete(_event("Delete")) == {"IsComplete": False}

    sfn.stop_execution.assert_called_once_with(executionArn=running_arn)


def test_is_complete_surfaces_failed_uninstall():
    sfn = MagicMock()
    sfn.list_executions.return_value = {"executions": []}
    sfn.describe_execution.return_value = {
        "status": "FAILED",
        "error": "RuntimeError",
        "cause": "helm uninstall keda failed: forbidden",
    }
    with (
        patch.dict(os.environ, _provider_env()),
        patch.object(teardown_provider, "_sfn", return_value=sfn),
        pytest.raises(RuntimeError, match="helm uninstall keda failed"),
    ):
        teardown_provider.is_complete(_event("Delete"))
