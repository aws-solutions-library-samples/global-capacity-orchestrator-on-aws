"""Unit tests for the helm-orchestrator custom-resource provider handler.

The handler (``lambda/helm-orchestrator/handler.py``) is a thin async
CloudFormation provider over the Helm-install Step Functions state machine. It
does no Helm/Kubernetes work itself, so these tests exercise exactly its two
jobs:

- ``on_event``: start a state-machine execution on Create/Update (passing the
  chart config through as the execution input) and no-op on Delete.
- ``is_complete``: poll the started execution and translate Step Functions
  terminal states into CloudFormation completion / failure.

The state machine itself is mocked — we assert on what the handler asks boto3
to do and how it interprets the responses, never on real AWS calls.
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest

from tests._lambda_imports import load_lambda_module

_STATE_MACHINE_ARN = "arn:aws:states:us-east-1:123456789012:stateMachine:HelmInstall"
_EXECUTION_ARN = "arn:aws:states:us-east-1:123456789012:execution:HelmInstall:abc-123"


@pytest.fixture
def orchestrator():
    """Load the handler with a mocked Step Functions client and env set.

    The handler reads ``STATE_MACHINE_ARN`` from the environment and builds its
    boto3 client lazily via ``_sfn()``; we patch ``_sfn`` to hand back a mock so
    no AWS call is ever made. Yields ``(handler, mock_sfn_client)``.
    """
    handler = load_lambda_module("helm-orchestrator")
    mock_client = MagicMock()
    with (
        patch.dict("os.environ", {"STATE_MACHINE_ARN": _STATE_MACHINE_ARN}),
        patch.object(handler, "_sfn", return_value=mock_client),
    ):
        yield handler, mock_client


# ---------------------------------------------------------------------------
# on_event
# ---------------------------------------------------------------------------


class TestOnEvent:
    def _props(self, **overrides):
        props = {
            "ClusterName": "gco-us-east-1",
            "Region": "us-east-1",
            "EnabledCharts": ["keda", "kueue"],
            "Charts": {"keda": {"enabled": True}},
            "KedaOperatorRoleArn": "arn:aws:iam::123456789012:role/keda",
        }
        props.update(overrides)
        return props

    @pytest.mark.parametrize("request_type", ["Create", "Update"])
    def test_starts_execution_on_create_and_update(self, orchestrator, request_type):
        handler, sfn = orchestrator
        sfn.start_execution.return_value = {"executionArn": _EXECUTION_ARN}

        result = handler.on_event(
            {"RequestType": request_type, "ResourceProperties": self._props()}
        )

        sfn.start_execution.assert_called_once()
        kwargs = sfn.start_execution.call_args.kwargs
        assert kwargs["stateMachineArn"] == _STATE_MACHINE_ARN
        # Execution input carries exactly what the per-chart tasks consume.

        sent = json.loads(kwargs["input"])
        assert sent == {
            "ClusterName": "gco-us-east-1",
            "Region": "us-east-1",
            "EnabledCharts": ["keda", "kueue"],
            "Charts": {"keda": {"enabled": True}},
            "KedaOperatorRoleArn": "arn:aws:iam::123456789012:role/keda",
        }
        # ExecutionArn is threaded back for is_complete (top level + Data).
        assert result["ExecutionArn"] == _EXECUTION_ARN
        assert result["Data"]["ExecutionArn"] == _EXECUTION_ARN
        assert result["PhysicalResourceId"]

    def test_delete_is_a_noop(self, orchestrator):
        handler, sfn = orchestrator

        result = handler.on_event(
            {"RequestType": "Delete", "PhysicalResourceId": "helm-install-charts"}
        )

        sfn.start_execution.assert_not_called()
        assert result == {"PhysicalResourceId": "helm-install-charts"}

    def test_preserves_existing_physical_id(self, orchestrator):
        handler, sfn = orchestrator
        sfn.start_execution.return_value = {"executionArn": _EXECUTION_ARN}

        result = handler.on_event(
            {
                "RequestType": "Update",
                "PhysicalResourceId": "existing-id",
                "ResourceProperties": self._props(),
            }
        )

        assert result["PhysicalResourceId"] == "existing-id"

    def test_optional_props_default_when_omitted(self, orchestrator):
        handler, sfn = orchestrator
        sfn.start_execution.return_value = {"executionArn": _EXECUTION_ARN}

        handler.on_event(
            {
                "RequestType": "Create",
                "ResourceProperties": {
                    "ClusterName": "gco-eu-west-1",
                    "Region": "eu-west-1",
                },
            }
        )

        sent = json.loads(sfn.start_execution.call_args.kwargs["input"])
        assert sent["EnabledCharts"] == []
        assert sent["Charts"] == {}
        assert sent["KedaOperatorRoleArn"] is None


# ---------------------------------------------------------------------------
# is_complete
# ---------------------------------------------------------------------------


class TestIsComplete:
    def test_delete_is_immediately_complete(self, orchestrator):
        handler, sfn = orchestrator

        result = handler.is_complete({"RequestType": "Delete"})

        assert result == {"IsComplete": True}
        sfn.describe_execution.assert_not_called()

    def test_succeeded_reports_complete(self, orchestrator):
        handler, sfn = orchestrator
        sfn.describe_execution.return_value = {"status": "SUCCEEDED"}

        result = handler.is_complete({"RequestType": "Create", "ExecutionArn": _EXECUTION_ARN})

        assert result["IsComplete"] is True
        assert result["Data"]["ExecutionArn"] == _EXECUTION_ARN
        sfn.describe_execution.assert_called_once_with(executionArn=_EXECUTION_ARN)

    def test_running_reports_not_complete(self, orchestrator):
        handler, sfn = orchestrator
        sfn.describe_execution.return_value = {"status": "RUNNING"}

        result = handler.is_complete({"RequestType": "Update", "ExecutionArn": _EXECUTION_ARN})

        assert result == {"IsComplete": False}

    @pytest.mark.parametrize("bad_status", ["FAILED", "TIMED_OUT", "ABORTED"])
    def test_terminal_failure_raises(self, orchestrator, bad_status):
        handler, sfn = orchestrator
        sfn.describe_execution.return_value = {"status": bad_status}

        with pytest.raises(RuntimeError, match=bad_status):
            handler.is_complete({"RequestType": "Create", "ExecutionArn": _EXECUTION_ARN})

    def test_reads_execution_arn_from_data_fallback(self, orchestrator):
        handler, sfn = orchestrator
        sfn.describe_execution.return_value = {"status": "SUCCEEDED"}

        result = handler.is_complete(
            {"RequestType": "Create", "Data": {"ExecutionArn": _EXECUTION_ARN}}
        )

        assert result["IsComplete"] is True
        sfn.describe_execution.assert_called_once_with(executionArn=_EXECUTION_ARN)

    def test_missing_execution_arn_raises(self, orchestrator):
        handler, sfn = orchestrator

        with pytest.raises(RuntimeError, match="without an ExecutionArn"):
            handler.is_complete({"RequestType": "Create"})

        sfn.describe_execution.assert_not_called()
