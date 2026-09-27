"""Tests for the CloudWatch Transaction Search custom resource handler.

``lambda/transaction-search/handler.py`` is the ``cr.Provider`` on-event
handler each regional stack runs while ``tracing.enabled`` and
``tracing.enable_transaction_search`` are true. Transaction Search is an
account-level setting shared with other workloads, so the contract under test
is deliberately one-directional: Create/Update switch the X-Ray trace segment
destination to CloudWatch Logs only when it is not already there, and Delete
never touches it. Real botocore clients run behind ``Stubber`` so every AWS
call, and its exact parameters, is asserted without a network.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from types import ModuleType
from typing import Any

import boto3
import pytest
from botocore.exceptions import EndpointConnectionError
from botocore.stub import Stubber

from tests._lambda_imports import load_lambda_module

_REGION = "us-east-1"
_ACCOUNT = "123456789012"
_PROPERTIES = {
    "ServiceToken": "arn:aws:lambda:us-east-1:123456789012:function:provider",
    "Region": _REGION,
    "AccountId": _ACCOUNT,
    "Partition": "aws",
    "ProjectName": "gco-test",
}


@pytest.fixture
def handler() -> ModuleType:
    return load_lambda_module("transaction-search")


class _Clients:
    """Stubbed X-Ray and CloudWatch Logs clients plus the factory calls seen."""

    def __init__(self) -> None:
        self.xray = boto3.client(
            "xray",
            region_name=_REGION,
            aws_access_key_id="testing",
            aws_secret_access_key="testing",  # nosec B106  # fake credentials for a stubbed client
        )
        self.logs = boto3.client(
            "logs",
            region_name=_REGION,
            aws_access_key_id="testing",
            aws_secret_access_key="testing",  # nosec B106  # fake credentials for a stubbed client
        )
        self.xray_stub = Stubber(self.xray)
        self.logs_stub = Stubber(self.logs)
        self.requests: list[tuple[str, dict[str, Any]]] = []

    def factory(self, service: str, **kwargs: Any) -> Any:
        self.requests.append((service, kwargs))
        return {"xray": self.xray, "logs": self.logs}[service]


@pytest.fixture
def clients(handler: ModuleType, monkeypatch: pytest.MonkeyPatch) -> Iterator[_Clients]:
    stubbed = _Clients()
    monkeypatch.setattr(handler.boto3, "client", stubbed.factory)
    with stubbed.xray_stub, stubbed.logs_stub:
        yield stubbed
        stubbed.xray_stub.assert_no_pending_responses()
        stubbed.logs_stub.assert_no_pending_responses()


def _event(request_type: str, **overrides: Any) -> dict[str, Any]:
    event: dict[str, Any] = {
        "RequestType": request_type,
        "ResourceProperties": dict(_PROPERTIES),
    }
    event.update(overrides)
    return event


def _expect_enable(handler: ModuleType, clients: _Clients, *, status: str = "PENDING") -> None:
    clients.logs_stub.add_response(
        "put_resource_policy",
        {},
        {
            "policyName": "gco-transaction-search-xray-access",
            "policyDocument": handler.transaction_search_resource_policy(
                partition="aws", region=_REGION, account=_ACCOUNT
            ),
        },
    )
    clients.xray_stub.add_response(
        "update_trace_segment_destination",
        {"Destination": "CloudWatchLogs", "Status": status},
        {"Destination": "CloudWatchLogs"},
    )


class TestResourcePolicy:
    def test_policy_admits_only_this_accounts_xray_to_the_span_log_groups(self, handler):
        document = json.loads(
            handler.transaction_search_resource_policy(
                partition="aws-us-gov", region="us-gov-west-1", account=_ACCOUNT
            )
        )

        assert document["Version"] == "2012-10-17"
        (statement,) = document["Statement"]
        assert statement == {
            "Sid": "TransactionSearchXRayAccess",
            "Effect": "Allow",
            "Principal": {"Service": "xray.amazonaws.com"},
            "Action": "logs:PutLogEvents",
            "Resource": [
                "arn:aws-us-gov:logs:us-gov-west-1:123456789012:log-group:aws/spans:*",
                "arn:aws-us-gov:logs:us-gov-west-1:123456789012:"
                "log-group:/aws/application-signals/data:*",
            ],
            "Condition": {
                "ArnLike": {"aws:SourceArn": "arn:aws-us-gov:xray:us-gov-west-1:123456789012:*"},
                "StringEquals": {"aws:SourceAccount": _ACCOUNT},
            },
        }

    def test_policy_is_compact_json(self, handler):
        rendered = handler.transaction_search_resource_policy(
            partition="aws", region=_REGION, account=_ACCOUNT
        )
        # CloudWatch Logs caps resource policies at 5120 characters; the
        # compact rendering keeps the document far below it.
        assert " " not in rendered
        assert len(rendered) < 1024


class TestCreateAndUpdate:
    def test_create_enables_transaction_search_when_xray_is_the_destination(self, handler, clients):
        clients.xray_stub.add_response(
            "get_trace_segment_destination", {"Destination": "XRay", "Status": "ACTIVE"}, {}
        )
        _expect_enable(handler, clients)

        result = handler.lambda_handler(_event("Create"), None)

        assert result == {
            "PhysicalResourceId": "gco-test-transaction-search-us-east-1",
            "Data": {"Destination": "CloudWatchLogs", "Status": "PENDING", "Changed": "true"},
        }
        # Both clients target the stack's Region with bounded standard retries.
        assert [service for service, _ in clients.requests] == ["xray", "logs"]
        for _, kwargs in clients.requests:
            assert kwargs["region_name"] == _REGION
            assert kwargs["config"] is handler._CLIENT_CONFIG
        assert handler._CLIENT_CONFIG.retries == {"max_attempts": 5, "mode": "standard"}

    @pytest.mark.parametrize("status", ["ACTIVE", "PENDING"])
    def test_create_changes_nothing_when_cloudwatch_logs_is_already_the_destination(
        self, handler, clients, status
    ):
        clients.xray_stub.add_response(
            "get_trace_segment_destination",
            {"Destination": "CloudWatchLogs", "Status": status},
            {},
        )

        result = handler.lambda_handler(_event("Create"), None)

        # No put_resource_policy / update call was stubbed: any attempt would
        # raise, so reaching here proves the shared setting was left alone.
        assert result["Data"] == {
            "Destination": "CloudWatchLogs",
            "Status": status,
            "Changed": "false",
        }

    def test_update_keeps_the_physical_id_and_re_enables_a_disabled_region(self, handler, clients):
        clients.xray_stub.add_response(
            "get_trace_segment_destination", {"Destination": "XRay", "Status": "PENDING"}, {}
        )
        _expect_enable(handler, clients, status="ACTIVE")

        result = handler.lambda_handler(
            _event("Update", PhysicalResourceId="existing-physical-id"), None
        )

        assert result == {
            "PhysicalResourceId": "existing-physical-id",
            "Data": {"Destination": "CloudWatchLogs", "Status": "ACTIVE", "Changed": "true"},
        }

    def test_sparse_api_responses_still_enable_and_report_strings(self, handler, clients):
        clients.xray_stub.add_response("get_trace_segment_destination", {}, {})
        clients.logs_stub.add_response("put_resource_policy", {})
        clients.xray_stub.add_response(
            "update_trace_segment_destination", {}, {"Destination": "CloudWatchLogs"}
        )
        event = _event("Create")
        del event["ResourceProperties"]["ProjectName"]

        result = handler.lambda_handler(event, None)

        assert result == {
            "PhysicalResourceId": "gco-transaction-search-us-east-1",
            "Data": {"Destination": "CloudWatchLogs", "Status": "", "Changed": "true"},
        }

    def test_property_values_are_trimmed(self, handler, clients):
        clients.xray_stub.add_response(
            "get_trace_segment_destination",
            {"Destination": "CloudWatchLogs", "Status": "ACTIVE"},
            {},
        )
        event = _event("Create")
        event["ResourceProperties"]["Region"] = f" {_REGION} "

        handler.lambda_handler(event, None)

        assert [kwargs["region_name"] for _, kwargs in clients.requests] == [_REGION, _REGION]


class TestDelete:
    def test_delete_never_touches_transaction_search(self, handler, monkeypatch):
        def _no_clients(*_args: Any, **_kwargs: Any) -> Any:
            raise AssertionError("Delete must not create AWS clients")

        monkeypatch.setattr(handler.boto3, "client", _no_clients)

        result = handler.lambda_handler(
            _event("Delete", PhysicalResourceId="gco-test-transaction-search-us-east-1"), None
        )

        assert result == {"PhysicalResourceId": "gco-test-transaction-search-us-east-1"}

    def test_delete_without_properties_still_answers(self, handler):
        result = handler.lambda_handler({"RequestType": "Delete"})

        assert result == {"PhysicalResourceId": "gco-transaction-search-unknown"}


class TestRejectedEvents:
    def test_unknown_request_type_is_rejected(self, handler):
        with pytest.raises(ValueError, match="Unsupported RequestType 'Rollback'"):
            handler.lambda_handler(_event("Rollback"), None)

    @pytest.mark.parametrize("name", ["Region", "AccountId", "Partition"])
    @pytest.mark.parametrize("value", [None, "", "   ", 123])
    def test_missing_or_malformed_properties_fail_before_any_aws_call(
        self, handler, monkeypatch, name, value
    ):
        monkeypatch.setattr(
            handler.boto3,
            "client",
            lambda *_args, **_kwargs: pytest.fail("no AWS client may be created"),
        )
        event = _event("Create")
        if value is None:
            del event["ResourceProperties"][name]
        else:
            event["ResourceProperties"][name] = value

        with pytest.raises(ValueError, match=f"property {name} must be a string"):
            handler.lambda_handler(event, None)


class TestFailures:
    def _assert_actionable(self, excinfo: pytest.ExceptionInfo[RuntimeError]) -> None:
        message = str(excinfo.value)
        assert message.startswith("Could not enable CloudWatch Transaction Search in us-east-1")
        assert "tracing.enable_transaction_search to false" in message
        assert "Transaction Search" in message

    def test_reading_the_destination_fails_with_the_opt_out_named(self, handler, clients):
        clients.xray_stub.add_client_error(
            "get_trace_segment_destination",
            service_error_code="InvalidRequestException",
            service_message="Transaction Search is not available",
        )

        with pytest.raises(RuntimeError) as excinfo:
            handler.lambda_handler(_event("Create"), None)

        self._assert_actionable(excinfo)
        assert "Transaction Search is not available" in str(excinfo.value)

    def test_resource_policy_quota_fails_before_the_destination_switch(self, handler, clients):
        clients.xray_stub.add_response(
            "get_trace_segment_destination", {"Destination": "XRay", "Status": "ACTIVE"}, {}
        )
        clients.logs_stub.add_client_error(
            "put_resource_policy",
            service_error_code="LimitExceededException",
            service_message="Resource limit exceeded.",
        )

        with pytest.raises(RuntimeError) as excinfo:
            handler.lambda_handler(_event("Create"), None)

        # update_trace_segment_destination was never stubbed, so the switch
        # was not attempted without the delivery policy in place.
        self._assert_actionable(excinfo)
        assert "LimitExceededException" in str(excinfo.value)

    def test_destination_switch_failure_is_reported(self, handler, clients):
        clients.xray_stub.add_response(
            "get_trace_segment_destination", {"Destination": "XRay", "Status": "ACTIVE"}, {}
        )
        clients.logs_stub.add_response("put_resource_policy", {})
        clients.xray_stub.add_client_error(
            "update_trace_segment_destination",
            service_error_code="AccessDeniedException",
            service_message="not authorized",
            http_status_code=403,
        )

        with pytest.raises(RuntimeError) as excinfo:
            handler.lambda_handler(_event("Update", PhysicalResourceId="existing"), None)

        self._assert_actionable(excinfo)

    def test_transport_errors_are_reported_the_same_way(self, handler, monkeypatch):
        class _Unreachable:
            def get_trace_segment_destination(self) -> dict[str, Any]:
                raise EndpointConnectionError(endpoint_url="https://xray.us-east-1.amazonaws.com")

        monkeypatch.setattr(handler.boto3, "client", lambda *_args, **_kwargs: _Unreachable())

        with pytest.raises(RuntimeError) as excinfo:
            handler.lambda_handler(_event("Create"), None)

        self._assert_actionable(excinfo)
        assert isinstance(excinfo.value.__cause__, EndpointConnectionError)
