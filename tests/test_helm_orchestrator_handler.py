"""Unit tests for the Helm orchestrator custom-resource provider.

The handler starts fire-and-forget Step Functions convergence on Create/Update,
persists the exact input and execution identity for topology consumers, and
keeps Delete as a no-op.
"""

from __future__ import annotations

import base64
import hashlib
import json
import random
import re
import zlib
from datetime import UTC, datetime
from unittest.mock import MagicMock, patch

import pytest
from botocore.exceptions import ClientError

from tests._lambda_imports import load_lambda_module

_STATE_MACHINE_ARN = "arn:aws:states:us-east-1:123456789012:stateMachine:HelmInstall"
_EXECUTION_ARN = "arn:aws:states:us-east-1:123456789012:execution:HelmInstall:abc-123"
_DEPLOYMENT_TIMESTAMP = "2026-07-18T01:02:03Z"
_START_DATE = datetime(2026, 7, 18, 1, 2, 4, tzinfo=UTC)


@pytest.fixture
def orchestrator():
    """Load the handler with mocked Step Functions/SSM clients and env set."""
    handler = load_lambda_module("helm-orchestrator")
    mock_client = MagicMock()
    real_prepare_teardown_fence = handler._prepare_teardown_fence
    with (
        patch.dict("os.environ", {"STATE_MACHINE_ARN": _STATE_MACHINE_ARN}),
        patch.object(handler, "_sfn", return_value=mock_client),
        # Tests that assert on SSM behavior override this with their own mock;
        # the fixture-level mock guarantees no test ever reaches real AWS.
        patch.object(handler, "_ssm", return_value=MagicMock()),
        patch.object(handler, "_prepare_teardown_fence") as fence_mock,
    ):
        fence_mock.real_implementation = real_prepare_teardown_fence
        yield handler, mock_client


class TestOnEvent:
    def _props(self, **overrides):
        props = {
            "ClusterName": "gco-us-east-1",
            "Region": "us-east-1",
            "RegistryRegion": "us-east-2",
            "ProjectName": "gco",
            "EnabledCharts": ["keda", "kueue"],
            "Charts": {"keda": {"enabled": True}},
            "KedaOperatorRoleArn": "arn:aws:iam::123456789012:role/keda",
            "DeploymentTimestamp": _DEPLOYMENT_TIMESTAMP,
        }
        props.update(overrides)
        return props

    @pytest.mark.parametrize("request_type", ["Create", "Update"])
    def test_starts_execution_on_create_and_update(self, orchestrator, request_type):
        handler, sfn = orchestrator
        sfn.start_execution.return_value = {
            "executionArn": _EXECUTION_ARN,
            "startDate": _START_DATE,
        }

        result = handler.on_event(
            {"RequestType": request_type, "ResourceProperties": self._props()}
        )

        sfn.start_execution.assert_called_once()
        kwargs = sfn.start_execution.call_args.kwargs
        assert kwargs["stateMachineArn"] == _STATE_MACHINE_ARN
        sent = json.loads(kwargs["input"])
        assert sent == {
            "ClusterName": "gco-us-east-1",
            "Region": "us-east-1",
            "RegistryRegion": "us-east-2",
            "ProjectName": "gco",
            "EnabledCharts": ["keda", "kueue"],
            "Charts": {"keda": {"enabled": True}},
            "KedaOperatorRoleArn": "arn:aws:iam::123456789012:role/keda",
            "ImageReplacements": {},
            "DeploymentToken": _DEPLOYMENT_TIMESTAMP,
        }
        assert kwargs["input"] == json.dumps(sent, sort_keys=True, separators=(",", ":"))
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
                    "RegistryRegion": "us-east-2",
                    "ProjectName": "gco",
                    "DeploymentTimestamp": _DEPLOYMENT_TIMESTAMP,
                },
            }
        )

        sent = json.loads(sfn.start_execution.call_args.kwargs["input"])
        assert sent["EnabledCharts"] == []
        assert sent["Charts"] == {}
        assert sent["KedaOperatorRoleArn"] is None
        assert sent["ImageReplacements"] == {}
        assert "EndpointGroupArn" not in sent
        assert sent["RegistryRegion"] == "us-east-2"
        assert sent["ProjectName"] == "gco"
        assert sent["DeploymentToken"] == _DEPLOYMENT_TIMESTAMP

    def test_persists_exact_input_and_execution_identity(self, orchestrator):
        handler, sfn = orchestrator
        ssm = MagicMock()
        sfn.start_execution.return_value = {
            "executionArn": _EXECUTION_ARN,
            "startDate": _START_DATE,
        }

        with patch.object(handler, "_ssm", return_value=ssm):
            handler.on_event(
                {
                    "RequestType": "Create",
                    "RequestId": "request-123",
                    "ResourceProperties": self._props(ProjectName="gco"),
                }
            )

        execution_input_text = sfn.start_execution.call_args.kwargs["input"]
        assert execution_input_text == json.dumps(
            json.loads(execution_input_text), sort_keys=True, separators=(",", ":")
        )

        assert ssm.put_parameter.call_count == 2
        input_write = ssm.put_parameter.call_args_list[0].kwargs
        assert input_write == {
            "Name": "/gco/addons/us-east-1/_input",
            "Value": handler._encode_replay_input(execution_input_text),
            "Type": "String",
            "Tier": "Intelligent-Tiering",
            "Overwrite": True,
        }
        # SSM rejects any value containing "{{"; the stored form must be
        # brace-free while decoding back to the exact execution input.
        assert "{{" not in input_write["Value"]
        decoded = zlib.decompress(base64.b64decode(input_write["Value"])).decode("utf-8")
        assert decoded == execution_input_text

        expected_metadata = {
            "execution_arn": _EXECUTION_ARN,
            "state_machine_arn": _STATE_MACHINE_ARN,
            "deployment_token": _DEPLOYMENT_TIMESTAMP,
            "cluster_name": "gco-us-east-1",
            "region": "us-east-1",
            "input_sha256": hashlib.sha256(execution_input_text.encode("utf-8")).hexdigest(),
            "started_at": int(_START_DATE.timestamp()),
        }
        execution_write = ssm.put_parameter.call_args_list[1].kwargs
        assert execution_write == {
            "Name": "/gco/addons/us-east-1/_execution",
            "Value": json.dumps(expected_metadata, sort_keys=True, separators=(",", ":")),
            "Type": "String",
            "Overwrite": True,
        }
        assert json.loads(execution_write["Value"]) == expected_metadata

    def test_placeholder_image_replacements_produce_a_brace_free_ssm_value(self, orchestrator):
        """Regression: raw ``{{PLACEHOLDER}}`` tokens made SSM reject the write.

        SSM String parameters refuse any value containing ``{{}}`` ("Parameter
        value can't nest another parameter"), which failed every deploy whose
        replay input carried image replacements.
        """
        handler, sfn = orchestrator
        ssm = MagicMock()
        sfn.start_execution.return_value = {
            "executionArn": _EXECUTION_ARN,
            "startDate": _START_DATE,
        }
        props = self._props(
            ImageReplacements={
                "{{HEALTH_MONITOR_IMAGE}}": "123456789012.dkr.ecr.us-east-1.amazonaws.com/x:1",
                "{{VPC_ENDPOINT_CIDR_BLOCKS}}": '- ipBlock:\n    cidr: "10.0.0.0/16"',
            }
        )

        with patch.object(handler, "_ssm", return_value=ssm):
            handler.on_event({"RequestType": "Create", "ResourceProperties": props})

        stored = ssm.put_parameter.call_args_list[0].kwargs["Value"]
        assert "{{" not in stored and "}}" not in stored
        decoded = zlib.decompress(base64.b64decode(stored)).decode("utf-8")
        assert decoded == sfn.start_execution.call_args.kwargs["input"]
        assert json.loads(decoded)["ImageReplacements"] == props["ImageReplacements"]

    def test_persistence_failure_reasons_are_bounded_for_cloudformation(self, orchestrator):
        """A parameter-echoing SSM error must not overflow the 4 KiB response."""
        handler, sfn = orchestrator
        ssm = MagicMock()
        ssm.put_parameter.side_effect = RuntimeError("boom " + "x" * 10_000)

        with (
            patch.object(handler, "_ssm", return_value=ssm),
            pytest.raises(RuntimeError) as excinfo,
        ):
            handler.on_event(
                {
                    "RequestType": "Update",
                    "ResourceProperties": self._props(ProjectName="gco"),
                }
            )

        assert len(str(excinfo.value)) < 1024
        assert "truncated" in str(excinfo.value)
        sfn.start_execution.assert_not_called()

    def test_request_id_produces_retry_stable_safe_execution_name(self, orchestrator):
        handler, sfn = orchestrator
        sfn.start_execution.return_value = {"executionArn": _EXECUTION_ARN}
        request_id = "request/with unsafe spaces:*?" + ("x" * 100)
        event = {
            "RequestType": "Create",
            "RequestId": request_id,
            "ResourceProperties": self._props(),
        }

        handler.on_event(event)
        handler.on_event(event)

        first_call, second_call = sfn.start_execution.call_args_list
        first_name = first_call.kwargs["name"]
        assert first_name == second_call.kwargs["name"] == handler._execution_name(request_id)
        assert len(first_name) <= 80
        assert re.fullmatch(r"[A-Za-z0-9_-]+", first_name)
        assert first_call.kwargs["input"] == second_call.kwargs["input"]

    def test_terminal_name_collision_advances_to_retry_generation(self, orchestrator):
        handler, sfn = orchestrator
        already_exists = ClientError(
            {"Error": {"Code": "ExecutionAlreadyExists", "Message": "exists"}},
            "StartExecution",
        )
        sfn.start_execution.side_effect = [
            already_exists,
            {"executionArn": f"{_EXECUTION_ARN}-retry", "startDate": _START_DATE},
        ]
        sfn.describe_execution.return_value = {"status": "ABORTED"}

        result = handler._start_or_adopt_execution(
            sfn,
            state_machine_arn=_STATE_MACHINE_ARN,
            execution_input_json="{}",
            request_id="request-123",
        )

        assert result["executionArn"].endswith("-retry")
        assert sfn.start_execution.call_args_list[1].kwargs["name"] == handler._execution_name(
            "request-123", 1
        )

    def test_running_identical_retry_is_adopted(self, orchestrator):
        handler, sfn = orchestrator
        already_exists = ClientError(
            {"Error": {"Code": "ExecutionAlreadyExists", "Message": "exists"}},
            "StartExecution",
        )
        sfn.start_execution.side_effect = already_exists
        sfn.describe_execution.return_value = {
            "status": "RUNNING",
            "input": "{}",
            "startDate": _START_DATE,
        }

        result = handler._start_or_adopt_execution(
            sfn,
            state_machine_arn=_STATE_MACHINE_ARN,
            execution_input_json="{}",
            request_id="request-123",
        )

        assert result["executionArn"] == handler._execution_arn(
            _STATE_MACHINE_ARN,
            handler._execution_name("request-123"),
        )
        sfn.start_execution.assert_called_once()

    def test_teardown_fence_is_cleared_on_create_and_blocks_update(self, orchestrator):
        handler, _ = orchestrator
        prepare_fence = handler._prepare_teardown_fence.real_implementation
        ssm = MagicMock()
        prepare_fence(
            ssm,
            request_type="Create",
            fence_name="/gco/addons/us-east-1/_teardown",
        )
        ssm.delete_parameter.assert_called_once_with(Name="/gco/addons/us-east-1/_teardown")

        ssm.reset_mock()
        ssm.get_parameter.return_value = {"Parameter": {"Value": "delete-request"}}
        with pytest.raises(RuntimeError, match="teardown fence is active"):
            prepare_fence(
                ssm,
                request_type="Update",
                fence_name="/gco/addons/us-east-1/_teardown",
            )

    def test_input_persistence_failure_prevents_execution_start(self, orchestrator):
        handler, sfn = orchestrator
        ssm = MagicMock()
        ssm.put_parameter.side_effect = RuntimeError("ssm unavailable")

        with (
            patch.object(handler, "_ssm", return_value=ssm),
            pytest.raises(RuntimeError, match="ssm unavailable"),
        ):
            handler.on_event(
                {
                    "RequestType": "Update",
                    "ResourceProperties": self._props(ProjectName="gco"),
                }
            )

        sfn.start_execution.assert_not_called()
        sfn.stop_execution.assert_not_called()
        assert ssm.put_parameter.call_count == 1

    def test_metadata_persistence_failure_stops_started_execution(self, orchestrator):
        handler, sfn = orchestrator
        ssm = MagicMock()
        ssm.put_parameter.side_effect = [{}, RuntimeError("ssm unavailable")]
        sfn.start_execution.return_value = {
            "executionArn": _EXECUTION_ARN,
            "startDate": _START_DATE,
        }

        with (
            patch.object(handler, "_ssm", return_value=ssm),
            pytest.raises(RuntimeError, match="ssm unavailable"),
        ):
            handler.on_event(
                {
                    "RequestType": "Update",
                    "ResourceProperties": self._props(ProjectName="gco"),
                }
            )

        sfn.start_execution.assert_called_once()
        sfn.stop_execution.assert_called_once_with(executionArn=_EXECUTION_ARN)
        assert ssm.put_parameter.call_count == 2

    def test_encoded_replay_input_over_advanced_parameter_limit_fails_before_writes(
        self, orchestrator
    ):
        handler, sfn = orchestrator
        ssm = MagicMock()
        # Only the ENCODED (zlib+base64) size is bounded — that is what SSM
        # stores. An incompressible payload keeps the encoding over the limit.
        incompressible = random.Random(241).randbytes(24 * 1024).hex()
        props = self._props(ImageReplacements={"oversized": incompressible})
        with (
            patch.object(handler, "_ssm", return_value=ssm),
            pytest.raises(ValueError, match="SSM Parameter Store supports at most 8192 bytes"),
        ):
            handler.on_event({"RequestType": "Update", "ResourceProperties": props})
        ssm.put_parameter.assert_not_called()
        sfn.start_execution.assert_not_called()

    def test_large_but_compressible_replay_input_is_accepted(self, orchestrator):
        # Regression (live: example-job validation run ex241-edf33111-r2):
        # enabling every optional chart pushed the raw canonical JSON past
        # 8 KiB while its zlib+base64 encoding stayed well under the limit.
        # The old raw-bytes gate rejected exactly that deployment.
        handler, sfn = orchestrator
        ssm = MagicMock()
        sfn.start_execution.return_value = {
            "executionArn": _EXECUTION_ARN,
            "startDate": _START_DATE,
        }
        charts = {
            f"chart-{index:02d}": {
                "namespace": "gco-system",
                "values": {
                    "image": {"repository": "registry.example/repeated/name"},
                    "resources": {
                        "requests": {"cpu": "100m", "memory": "128Mi"},
                        "limits": {"cpu": "500m", "memory": "512Mi"},
                    },
                    "tolerations": [],
                    "podAnnotations": {"gco.aws/component": "optional-chart"},
                },
            }
            for index in range(40)
        }
        props = self._props(Charts=charts, EnabledCharts=sorted(charts))
        raw_input = {
            "ClusterName": props["ClusterName"],
            "Region": props["Region"],
            "RegistryRegion": props["RegistryRegion"],
            "ProjectName": props["ProjectName"],
            "EnabledCharts": props["EnabledCharts"],
            "Charts": props["Charts"],
            "KedaOperatorRoleArn": props.get("KedaOperatorRoleArn"),
            "ImageReplacements": props.get("ImageReplacements", {}),
            "DeploymentToken": props["DeploymentTimestamp"],
        }
        raw_size = len(handler._canonical_json(raw_input).encode())
        assert raw_size > 8 * 1024, "the fixture must reproduce the >8KiB raw shape"

        with patch.object(handler, "_ssm", return_value=ssm):
            result = handler.on_event({"RequestType": "Update", "ResourceProperties": props})

        assert result["Data"]["ExecutionArn"] == _EXECUTION_ARN
        encoded = ssm.put_parameter.call_args_list[0].kwargs["Value"]
        assert len(encoded) <= 8 * 1024, "the persisted encoding must fit the SSM bound"

    def test_missing_deployment_timestamp_fails_before_start(self, orchestrator):
        handler, sfn = orchestrator
        props = self._props()
        del props["DeploymentTimestamp"]

        with pytest.raises(KeyError, match="DeploymentTimestamp"):
            handler.on_event({"RequestType": "Create", "ResourceProperties": props})

        sfn.start_execution.assert_not_called()

    def test_endpoint_group_arn_is_forwarded_when_present(self, orchestrator):
        handler, sfn = orchestrator
        sfn.start_execution.return_value = {"executionArn": _EXECUTION_ARN}
        endpoint_group = (
            "arn:aws:globalaccelerator::123456789012:accelerator/a/listener/b/endpoint-group/c"
        )

        handler.on_event(
            {
                "RequestType": "Create",
                "ResourceProperties": self._props(EndpointGroupArn=endpoint_group),
            }
        )

        sent = json.loads(sfn.start_execution.call_args.kwargs["input"])
        assert sent["EndpointGroupArn"] == endpoint_group

    def test_unconfirmed_rollback_is_surfaced_over_the_persistence_error(self, orchestrator):
        # If the untracked execution cannot be proven stopped, that is the more
        # dangerous condition and must be the error CloudFormation sees.
        handler, sfn = orchestrator
        ssm = MagicMock()
        ssm.put_parameter.side_effect = [{}, RuntimeError("ssm unavailable")]
        sfn.start_execution.return_value = {"executionArn": _EXECUTION_ARN}
        sfn.stop_execution.side_effect = ClientError(
            {"Error": {"Code": "AccessDeniedException"}}, "StopExecution"
        )

        with (
            patch.object(handler, "_ssm", return_value=ssm),
            pytest.raises(RuntimeError, match="Could not confirm untracked execution") as info,
        ):
            handler.on_event({"RequestType": "Update", "ResourceProperties": self._props()})

        assert isinstance(info.value.__cause__, ClientError)


class TestClients:
    def test_clients_are_built_with_boto3(self):
        handler = load_lambda_module("helm-orchestrator")
        built = []
        with patch.object(handler.boto3, "client", side_effect=lambda name: built.append(name)):
            handler._sfn()
            handler._ssm()
        assert built == ["stepfunctions", "ssm"]


class TestExecutionHelpers:
    def test_execution_arn_rejects_a_non_state_machine_arn(self, orchestrator):
        handler, _ = orchestrator
        with pytest.raises(ValueError, match="Invalid Step Functions state machine ARN"):
            handler._execution_arn("arn:aws:states:us-east-1:123456789012:activity:x", "name")

    def test_started_at_accepts_epoch_numbers(self, orchestrator):
        handler, _ = orchestrator
        assert handler._started_at({"startDate": 1_784_000_000.7}) == 1_784_000_000
        assert handler._started_at({"startDate": 1_784_000_000}) == 1_784_000_000

    def test_start_failures_other_than_a_duplicate_propagate(self, orchestrator):
        handler, sfn = orchestrator
        sfn.start_execution.side_effect = ClientError(
            {"Error": {"Code": "StateMachineDoesNotExist"}}, "StartExecution"
        )

        with pytest.raises(ClientError, match="StateMachineDoesNotExist"):
            handler._start_or_adopt_execution(
                sfn,
                state_machine_arn=_STATE_MACHINE_ARN,
                execution_input_json="{}",
                request_id="req-1",
            )
        sfn.describe_execution.assert_not_called()

    def test_running_retry_with_different_input_is_refused(self, orchestrator):
        handler, sfn = orchestrator
        sfn.start_execution.side_effect = ClientError(
            {"Error": {"Code": "ExecutionAlreadyExists"}}, "StartExecution"
        )
        sfn.describe_execution.return_value = {"status": "RUNNING", "input": '{"other":1}'}

        with pytest.raises(RuntimeError, match="non-identical input"):
            handler._start_or_adopt_execution(
                sfn,
                state_machine_arn=_STATE_MACHINE_ARN,
                execution_input_json='{"mine":1}',
                request_id="req-1",
            )

    def test_generation_search_is_bounded(self, orchestrator):
        handler, sfn = orchestrator
        sfn.start_execution.side_effect = ClientError(
            {"Error": {"Code": "ExecutionAlreadyExists"}}, "StartExecution"
        )
        sfn.describe_execution.return_value = {"status": "SUCCEEDED"}

        with pytest.raises(RuntimeError, match="Exhausted 100 retry-safe execution generations"):
            handler._start_or_adopt_execution(
                sfn,
                state_machine_arn=_STATE_MACHINE_ARN,
                execution_input_json="{}",
                request_id="req-1",
            )
        assert sfn.start_execution.call_count == 100

    def test_stop_and_wait_polls_then_gives_up(self, orchestrator):
        handler, sfn = orchestrator
        sfn.describe_execution.return_value = {"status": "RUNNING"}
        clock = iter([0.0, 0.0, 5.0, 20.0])

        with (
            patch.object(handler.time, "monotonic", side_effect=lambda: next(clock)),
            patch.object(handler.time, "sleep") as sleep,
            pytest.raises(TimeoutError, match="remained RUNNING after StopExecution"),
        ):
            handler._stop_execution_and_wait(sfn, _EXECUTION_ARN)

        sfn.stop_execution.assert_called_once_with(executionArn=_EXECUTION_ARN)
        assert sfn.describe_execution.call_count == 2
        assert sleep.call_count == 2


class TestTeardownFence:
    def _fence(self, orchestrator):
        handler, _ = orchestrator
        return handler._prepare_teardown_fence.real_implementation

    def test_create_tolerates_an_absent_fence(self, orchestrator):
        ssm = MagicMock()
        ssm.delete_parameter.side_effect = ClientError(
            {"Error": {"Code": "ParameterNotFound"}}, "DeleteParameter"
        )
        self._fence(orchestrator)(ssm, request_type="Create", fence_name="/gco/f")

    def test_create_surfaces_other_delete_failures(self, orchestrator):
        ssm = MagicMock()
        ssm.delete_parameter.side_effect = ClientError(
            {"Error": {"Code": "AccessDeniedException"}}, "DeleteParameter"
        )
        with pytest.raises(ClientError, match="AccessDeniedException"):
            self._fence(orchestrator)(ssm, request_type="Create", fence_name="/gco/f")

    def test_update_proceeds_when_no_fence_exists(self, orchestrator):
        ssm = MagicMock()
        ssm.get_parameter.side_effect = ClientError(
            {"Error": {"Code": "ParameterNotFound"}}, "GetParameter"
        )
        self._fence(orchestrator)(ssm, request_type="Update", fence_name="/gco/f")

    def test_update_surfaces_other_read_failures(self, orchestrator):
        ssm = MagicMock()
        ssm.get_parameter.side_effect = ClientError(
            {"Error": {"Code": "ThrottlingException"}}, "GetParameter"
        )
        with pytest.raises(ClientError, match="ThrottlingException"):
            self._fence(orchestrator)(ssm, request_type="Update", fence_name="/gco/f")
