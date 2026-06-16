"""Unit tests for the helm-orchestrator provider handlers.

The orchestrator is the thin async custom-resource provider for the helm
install state machine: ``on_event`` starts an execution, ``is_complete`` polls
it. It does no Helm/Kubernetes work, so these tests just verify it drives
Step Functions correctly and maps execution status to CloudFormation
completion (raising on a non-success terminal state).
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from tests._lambda_imports import load_lambda_module

orch = load_lambda_module("helm-orchestrator")


def _on_event(request_type: str, **props):
    base_props = {
        "ClusterName": "gco-us-east-1",
        "Region": "us-east-1",
        "EnabledCharts": ["keda"],
        "Charts": {},
        "KedaOperatorRoleArn": "arn:aws:iam::123456789012:role/keda",
    }
    base_props.update(props)
    return {"RequestType": request_type, "ResourceProperties": base_props}


class TestOnEvent:
    def test_create_starts_execution(self, monkeypatch):
        monkeypatch.setenv("STATE_MACHINE_ARN", "arn:aws:states:us-east-1:1:stateMachine:helm")
        mock_sfn = MagicMock()
        mock_sfn.start_execution.return_value = {
            "executionArn": "arn:aws:states:us-east-1:1:execution:helm:abc"
        }
        with patch.object(orch, "_sfn", return_value=mock_sfn):
            out = orch.on_event(_on_event("Create"))

        assert out["ExecutionArn"] == "arn:aws:states:us-east-1:1:execution:helm:abc"
        args, kwargs = mock_sfn.start_execution.call_args
        assert kwargs["stateMachineArn"].endswith("stateMachine:helm")
        # The execution input carries exactly what the chart tasks need.
        import json as _json

        sent = _json.loads(kwargs["input"])
        assert sent["EnabledCharts"] == ["keda"]
        assert sent["ClusterName"] == "gco-us-east-1"

    def test_delete_is_noop(self, monkeypatch):
        monkeypatch.setenv("STATE_MACHINE_ARN", "arn:aws:states:us-east-1:1:stateMachine:helm")
        mock_sfn = MagicMock()
        with patch.object(orch, "_sfn", return_value=mock_sfn):
            out = orch.on_event(_on_event("Delete"))
        assert "PhysicalResourceId" in out
        mock_sfn.start_execution.assert_not_called()


class TestIsComplete:
    _ARN = "arn:aws:states:us-east-1:1:execution:helm:abc"

    def _event(self, request_type="Create"):
        return {"RequestType": request_type, "ExecutionArn": self._ARN}

    def test_running_not_complete(self):
        mock_sfn = MagicMock()
        mock_sfn.describe_execution.return_value = {"status": "RUNNING"}
        with patch.object(orch, "_sfn", return_value=mock_sfn):
            out = orch.is_complete(self._event())
        assert out["IsComplete"] is False

    def test_succeeded_complete(self):
        mock_sfn = MagicMock()
        mock_sfn.describe_execution.return_value = {"status": "SUCCEEDED"}
        with patch.object(orch, "_sfn", return_value=mock_sfn):
            out = orch.is_complete(self._event())
        assert out["IsComplete"] is True

    @pytest.mark.parametrize("status", ["FAILED", "TIMED_OUT", "ABORTED"])
    def test_terminal_failure_raises(self, status):
        mock_sfn = MagicMock()
        mock_sfn.describe_execution.return_value = {"status": status}
        with (
            patch.object(orch, "_sfn", return_value=mock_sfn),
            pytest.raises(RuntimeError, match=status),
        ):
            orch.is_complete(self._event())

    def test_delete_is_complete(self):
        out = orch.is_complete({"RequestType": "Delete"})
        assert out["IsComplete"] is True

    def test_missing_execution_arn_raises(self):
        with pytest.raises(RuntimeError, match="without an ExecutionArn"):
            orch.is_complete({"RequestType": "Create"})
