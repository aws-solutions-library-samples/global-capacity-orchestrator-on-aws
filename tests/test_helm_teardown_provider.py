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
            "RegistryRegion": "us-east-2",
            "ProjectName": "gco",
            "EndpointGroupArn": "arn:aws:globalaccelerator::123456789012:accelerator/a/listener/b/endpoint-group/c",
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
    ssm = MagicMock()
    sfn.list_executions.return_value = {"executions": []}
    with (
        patch.dict(os.environ, _provider_env()),
        patch.object(teardown_provider, "_sfn", return_value=sfn),
        patch.object(teardown_provider, "_ssm", return_value=ssm),
    ):
        result = teardown_provider.on_event(event)

    assert result == {"PhysicalResourceId": "helm-teardown"}
    ssm.put_parameter.assert_called_once_with(
        Name="/gco/addons/us-east-1/_teardown",
        Value=teardown_provider._execution_name(event),
        Type="String",
        Overwrite=True,
    )
    kwargs = sfn.start_execution.call_args.kwargs
    assert kwargs["stateMachineArn"] == _STATE_MACHINE_ARN
    assert kwargs["name"] == teardown_provider._execution_name(event)
    execution_input = json.loads(kwargs["input"])
    assert execution_input["EnabledCharts"] == ["keda", "kueue"]
    assert execution_input["RegistryRegion"] == "us-east-2"
    assert execution_input["ProjectName"] == "gco"
    assert execution_input["EndpointGroupArn"].endswith("/endpoint-group/c")
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
        patch.object(teardown_provider, "_ssm", return_value=MagicMock()),
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

    stopped = teardown_provider._stop_running_install_executions(sfn, _INSTALL_STATE_MACHINE_ARN)

    assert stopped == 1
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
        patch.object(teardown_provider, "_ssm", return_value=MagicMock()),
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

    assert sfn.list_executions.call_count == 0


def test_drain_task_stops_execution_missed_by_initial_snapshot():
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
        assert teardown_provider.drain_install_executions({}) == {"StoppedExecutions": 1}

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


def test_clients_are_built_with_boto3():
    ssm_client, sfn_client = MagicMock(), MagicMock()

    def route(service, **kwargs):
        if service == "ssm":
            assert kwargs == {"region_name": "eu-west-1"}
            return ssm_client
        assert service == "stepfunctions"
        assert kwargs == {}
        return sfn_client

    with patch.object(teardown_provider.boto3, "client", side_effect=route):
        assert teardown_provider._sfn() is sfn_client
        assert teardown_provider._ssm("eu-west-1") is ssm_client


def test_execution_arn_rejects_a_non_state_machine_arn():
    with pytest.raises(ValueError, match="Invalid Step Functions state machine ARN"):
        teardown_provider._execution_arn(
            "arn:aws:states:us-east-1:123456789012:activity:not-a-state-machine", "x"
        )
    with pytest.raises(ValueError, match="Invalid Step Functions state machine ARN"):
        teardown_provider._execution_arn("arn:aws:states:us-east-1:123456789012:stateMachine:", "x")


def test_is_complete_is_trivially_true_outside_delete():
    sfn = MagicMock()
    with patch.object(teardown_provider, "_sfn", return_value=sfn):
        assert teardown_provider.is_complete(_event("Update")) == {"IsComplete": True}
    sfn.describe_execution.assert_not_called()


def test_delete_without_an_endpoint_group_omits_the_key():
    event = _event("Delete")
    del event["ResourceProperties"]["EndpointGroupArn"]
    sfn = MagicMock()
    sfn.list_executions.return_value = {"executions": []}
    with (
        patch.dict(os.environ, _provider_env()),
        patch.object(teardown_provider, "_sfn", return_value=sfn),
        patch.object(teardown_provider, "_ssm", return_value=MagicMock()),
    ):
        teardown_provider.on_event(event)

    execution_input = json.loads(sfn.start_execution.call_args.kwargs["input"])
    assert "EndpointGroupArn" not in execution_input


def test_start_failures_other_than_a_duplicate_block_teardown():
    error = ClientError(
        {"Error": {"Code": "AccessDeniedException", "Message": "no"}}, "StartExecution"
    )
    sfn = MagicMock()
    sfn.list_executions.return_value = {"executions": []}
    sfn.start_execution.side_effect = error
    with (
        patch.dict(os.environ, _provider_env()),
        patch.object(teardown_provider, "_sfn", return_value=sfn),
        patch.object(teardown_provider, "_ssm", return_value=MagicMock()),
        pytest.raises(ClientError, match="AccessDeniedException"),
    ):
        teardown_provider.on_event(_event("Delete"))


def _execution(name: str) -> str:
    return f"arn:aws:states:us-east-1:123456789012:execution:gco-helm-install:{name}"


def test_stop_follows_list_pagination():
    sfn = MagicMock()
    sfn.list_executions.side_effect = [
        {"executions": [{"executionArn": _execution("a")}], "nextToken": "page2"},
        {"executions": [{"executionArn": _execution("b")}]},
    ]

    stopped = teardown_provider._stop_running_install_executions(sfn, _INSTALL_STATE_MACHINE_ARN)

    assert stopped == 2
    assert sfn.list_executions.call_args_list[1].kwargs["nextToken"] == "page2"
    assert [c.kwargs["executionArn"] for c in sfn.stop_execution.call_args_list] == [
        _execution("a"),
        _execution("b"),
    ]


@pytest.mark.parametrize("code", ["ExecutionDoesNotExist", "ExecutionNotRunning"])
def test_stop_tolerates_an_execution_that_already_finished(code):
    sfn = MagicMock()
    sfn.list_executions.return_value = {"executions": [{"executionArn": _execution("gone")}]}
    sfn.stop_execution.side_effect = ClientError({"Error": {"Code": code}}, "StopExecution")

    assert teardown_provider._stop_running_install_executions(sfn, _INSTALL_STATE_MACHINE_ARN) == 1
    sfn.describe_execution.assert_not_called()


def test_stop_reraises_unexpected_failures():
    sfn = MagicMock()
    sfn.list_executions.return_value = {"executions": [{"executionArn": _execution("x")}]}
    sfn.stop_execution.side_effect = ClientError(
        {"Error": {"Code": "AccessDeniedException"}}, "StopExecution"
    )

    with pytest.raises(ClientError, match="AccessDeniedException"):
        teardown_provider._stop_running_install_executions(sfn, _INSTALL_STATE_MACHINE_ARN)


def test_validation_failure_on_a_still_running_execution_blocks_teardown():
    # A ValidationException is only forgiven once the execution is proven
    # terminal; if it is still RUNNING, the stop genuinely failed.
    sfn = MagicMock()
    sfn.list_executions.return_value = {"executions": [{"executionArn": _execution("x")}]}
    sfn.stop_execution.side_effect = ClientError(
        {"Error": {"Code": "ValidationException"}}, "StopExecution"
    )
    sfn.describe_execution.return_value = {"status": "RUNNING"}

    with pytest.raises(ClientError, match="ValidationException"):
        teardown_provider._stop_running_install_executions(sfn, _INSTALL_STATE_MACHINE_ARN)


def test_validation_failure_re_read_tolerates_a_vanished_execution():
    sfn = MagicMock()
    sfn.list_executions.return_value = {"executions": [{"executionArn": _execution("x")}]}
    sfn.stop_execution.side_effect = ClientError(
        {"Error": {"Code": "ValidationException"}}, "StopExecution"
    )
    sfn.describe_execution.side_effect = ClientError(
        {"Error": {"Code": "ExecutionDoesNotExist"}}, "DescribeExecution"
    )

    assert teardown_provider._stop_running_install_executions(sfn, _INSTALL_STATE_MACHINE_ARN) == 1


def test_validation_failure_re_read_reraises_other_describe_errors():
    sfn = MagicMock()
    sfn.list_executions.return_value = {"executions": [{"executionArn": _execution("x")}]}
    sfn.stop_execution.side_effect = ClientError(
        {"Error": {"Code": "ValidationException"}}, "StopExecution"
    )
    sfn.describe_execution.side_effect = ClientError(
        {"Error": {"Code": "ThrottlingException"}}, "DescribeExecution"
    )

    with pytest.raises(ClientError, match="ThrottlingException"):
        teardown_provider._stop_running_install_executions(sfn, _INSTALL_STATE_MACHINE_ARN)
