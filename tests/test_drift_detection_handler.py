"""Tests for the scheduled CloudFormation drift-detection Lambda.

``lambda/drift-detection/handler.py`` starts DetectStackDrift, polls the
asynchronous detection to a terminal state and publishes an SNS alert when
the stack has drifted (or when detection itself failed). These tests pin the
environment contract, the three outcomes, the poll loop's terminal/timeout
behaviour, server-side drift filtering with pagination, and the 100-character
SNS subject limit. The stack-side wiring (rule, topic, IAM) is covered by
``tests/test_drift_detection.py``.
"""

from __future__ import annotations

import json
import logging
from unittest.mock import MagicMock, call, patch

import pytest

from tests._lambda_imports import load_lambda_module

_STACK = "gco-regional-us-east-1"
_TOPIC = "arn:aws:sns:us-east-1:123456789012:gco-drift-alerts"


@pytest.fixture
def handler(monkeypatch):
    monkeypatch.setenv("STACK_NAME", _STACK)
    monkeypatch.setenv("SNS_TOPIC_ARN", _TOPIC)
    monkeypatch.setenv("REGION", "us-east-1")
    monkeypatch.setenv("POLL_INTERVAL_SECONDS", "0")
    monkeypatch.setenv("POLL_MAX_ATTEMPTS", "3")
    module = load_lambda_module("drift-detection")
    monkeypatch.setattr(module.time, "sleep", lambda _seconds: None)
    return module


@pytest.fixture
def aws(handler):
    """Route ``boto3.client`` to CloudFormation and SNS stubs."""
    cfn, sns = MagicMock(), MagicMock()
    cfn.detect_stack_drift.return_value = {"StackDriftDetectionId": "det-1"}
    cfn.describe_stack_drift_detection_status.return_value = {
        "DetectionStatus": "DETECTION_COMPLETE",
        "StackDriftStatus": "IN_SYNC",
    }
    paginator = MagicMock()
    paginator.paginate.return_value = [{"StackResourceDrifts": []}]
    cfn.get_paginator.return_value = paginator

    def route(service, **kwargs):
        assert kwargs == {"region_name": "us-east-1"}
        return {"cloudformation": cfn, "sns": sns}[service]

    with patch.object(handler.boto3, "client", side_effect=route):
        yield cfn, sns


def _published_message(sns) -> dict:
    return json.loads(sns.publish.call_args.kwargs["Message"])


class TestEnvironmentContract:
    def test_stack_name_is_required(self, handler, monkeypatch):
        monkeypatch.delenv("STACK_NAME")
        with pytest.raises(ValueError, match="STACK_NAME"):
            handler.lambda_handler({}, None)

    def test_topic_arn_is_required(self, handler, monkeypatch):
        monkeypatch.setenv("SNS_TOPIC_ARN", "")
        with pytest.raises(ValueError, match="SNS_TOPIC_ARN"):
            handler.lambda_handler({}, None)

    def test_region_falls_back_to_the_lambda_runtime_variable(self, handler, monkeypatch, aws):
        monkeypatch.delenv("REGION")
        monkeypatch.setenv("AWS_REGION", "us-east-1")

        result = handler.lambda_handler({}, None)

        assert result["stack_name"] == _STACK


class TestOutcomes:
    def test_in_sync_stack_publishes_nothing(self, handler, aws, caplog):
        cfn, sns = aws

        with caplog.at_level(logging.INFO):
            result = handler.lambda_handler({"source": "aws.events"}, None)

        assert result == {
            "stack_name": _STACK,
            "detection_status": "DETECTION_COMPLETE",
            "stack_drift_status": "IN_SYNC",
            "drift_published": False,
        }
        cfn.detect_stack_drift.assert_called_once_with(StackName=_STACK)
        sns.publish.assert_not_called()
        cfn.get_paginator.assert_not_called()
        assert "is IN_SYNC" in caplog.text

    def test_drifted_stack_publishes_the_drifted_resources(self, handler, aws):
        cfn, sns = aws
        cfn.describe_stack_drift_detection_status.return_value = {
            "DetectionStatus": "DETECTION_COMPLETE",
            "StackDriftStatus": "DRIFTED",
        }
        cfn.get_paginator.return_value.paginate.return_value = [
            {
                "StackResourceDrifts": [
                    {
                        "LogicalResourceId": "JobsTable",
                        "PhysicalResourceId": "gco-jobs",
                        "ResourceType": "AWS::DynamoDB::Table",
                        "StackResourceDriftStatus": "MODIFIED",
                    }
                ]
            },
            {
                "StackResourceDrifts": [
                    {
                        "LogicalResourceId": "Alarm",
                        "StackResourceDriftStatus": "DELETED",
                    }
                ]
            },
        ]

        result = handler.lambda_handler({}, None)

        assert result == {
            "stack_name": _STACK,
            "detection_status": "DETECTION_COMPLETE",
            "stack_drift_status": "DRIFTED",
            "drifted_resource_count": 2,
            "drift_published": True,
        }
        # Filtered server-side so large stacks only return what drifted.
        cfn.get_paginator.return_value.paginate.assert_called_once_with(
            StackName=_STACK,
            StackResourceDriftStatusFilters=["MODIFIED", "DELETED", "NOT_CHECKED"],
        )
        publish = sns.publish.call_args.kwargs
        assert publish["TopicArn"] == _TOPIC
        assert publish["Subject"] == f"[GCO] Drift detected in stack {_STACK}"
        message = _published_message(sns)
        assert message["region"] == "us-east-1"
        assert message["drifted_resource_count"] == 2
        assert message["drifted_resources"] == [
            {
                "logical_id": "JobsTable",
                "physical_id": "gco-jobs",
                "resource_type": "AWS::DynamoDB::Table",
                "drift_status": "MODIFIED",
            },
            {
                "logical_id": "Alarm",
                "physical_id": "",
                "resource_type": "",
                "drift_status": "DELETED",
            },
        ]

    def test_failed_detection_is_alerted_as_a_failure(self, handler, aws):
        cfn, sns = aws
        cfn.describe_stack_drift_detection_status.return_value = {
            "DetectionStatus": "DETECTION_FAILED",
            "DetectionStatusReason": "Stack is being updated",
        }

        result = handler.lambda_handler({}, None)

        assert result == {
            "stack_name": _STACK,
            "detection_status": "DETECTION_FAILED",
            "stack_drift_status": None,
            "drift_published": True,
        }
        cfn.get_paginator.assert_not_called()
        assert sns.publish.call_args.kwargs["Subject"] == (
            f"[GCO] Drift detection FAILED for {_STACK}"
        )
        assert _published_message(sns) == {
            "stack_name": _STACK,
            "region": "us-east-1",
            "detection_status": "DETECTION_FAILED",
            "reason": "Stack is being updated",
        }

    def test_failed_detection_without_a_reason_still_alerts(self, handler, aws):
        cfn, sns = aws
        cfn.describe_stack_drift_detection_status.return_value = {
            "DetectionStatus": "DETECTION_FAILED"
        }

        handler.lambda_handler({}, None)

        assert _published_message(sns)["reason"] == "Unknown detection failure"


class TestPolling:
    def test_polls_until_a_terminal_status(self, handler):
        cfn = MagicMock()
        cfn.describe_stack_drift_detection_status.side_effect = [
            {"DetectionStatus": "DETECTION_IN_PROGRESS"},
            {"DetectionStatus": "DETECTION_IN_PROGRESS"},
            {"DetectionStatus": "DETECTION_COMPLETE", "StackDriftStatus": "IN_SYNC"},
        ]
        sleeps: list[int] = []

        with patch.object(handler.time, "sleep", side_effect=sleeps.append):
            response = handler._poll_detection_status(cfn, "det-1", 10, 5)

        assert response["DetectionStatus"] == "DETECTION_COMPLETE"
        assert (
            cfn.describe_stack_drift_detection_status.call_args_list
            == [call(StackDriftDetectionId="det-1")] * 3
        )
        assert sleeps == [10, 10]

    def test_timeout_returns_the_last_status_and_warns(self, handler, caplog):
        cfn = MagicMock()
        cfn.describe_stack_drift_detection_status.return_value = {
            "DetectionStatus": "DETECTION_IN_PROGRESS"
        }

        with caplog.at_level(logging.WARNING):
            response = handler._poll_detection_status(cfn, "det-1", 0, 2)

        assert response == {"DetectionStatus": "DETECTION_IN_PROGRESS"}
        assert cfn.describe_stack_drift_detection_status.call_count == 2
        assert "did not complete within 2 polls" in caplog.text

    def test_poll_tuning_comes_from_the_environment(self, handler, aws, monkeypatch):
        cfn, _ = aws
        monkeypatch.setenv("POLL_MAX_ATTEMPTS", "2")
        cfn.describe_stack_drift_detection_status.return_value = {
            "DetectionStatus": "DETECTION_IN_PROGRESS"
        }

        handler.lambda_handler({}, None)

        assert cfn.describe_stack_drift_detection_status.call_count == 2


class TestPublishAlert:
    def test_subject_is_truncated_to_the_sns_limit(self, handler):
        sns = MagicMock()

        handler._publish_alert(sns, _TOPIC, "x" * 150, {"when": handler.time})

        publish = sns.publish.call_args.kwargs
        assert publish["TopicArn"] == _TOPIC
        assert len(publish["Subject"]) == 100
        # Non-JSON values fall back to str() rather than failing the alert.
        assert json.loads(publish["Message"])["when"].startswith("<module 'time'")
