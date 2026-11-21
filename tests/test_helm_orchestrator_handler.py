"""Unit tests for the Helm orchestrator custom-resource provider.

The handler starts fire-and-forget Step Functions convergence on Create/Update,
persists the exact input and execution identity for topology consumers, and
keeps Delete as a no-op. An input whose zlib+base64 encoding exceeds one 8 KiB
SSM value is stored in parts written before their ``_input`` header, the parts
of a replaced header are deleted best-effort, and only Step Functions' 256 KiB
input limit refuses a deployment.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import logging
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
_INPUT_NAME = "/gco/addons/us-east-1/_input"
_EXECUTION_RECORD_NAME = "/gco/addons/us-east-1/_execution"


def _canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def _encoded(execution_input_json):
    return base64.b64encode(zlib.compress(execution_input_json.encode("utf-8"), 9)).decode("ascii")


def _execution_input(props):
    """The execution input ``on_event`` builds from ``props`` (object Charts)."""
    return {
        "ClusterName": props["ClusterName"],
        "Region": props["Region"],
        "RegistryRegion": props["RegistryRegion"],
        "ProjectName": props["ProjectName"],
        "EnabledCharts": props.get("EnabledCharts", []),
        "Charts": props.get("Charts", {}),
        "KedaOperatorRoleArn": props.get("KedaOperatorRoleArn"),
        "ImageReplacements": props.get("ImageReplacements", {}),
        "DeploymentToken": props["DeploymentTimestamp"],
    }


def _image_replacements(count):
    """Image replacements shaped like a full deployment's: ECR image URIs
    pinned by 64-hex digests, which zlib can barely shrink."""
    return {
        f"{{{{IMAGE_{index:03d}}}}}": (
            f"123456789012.dkr.ecr.us-east-1.amazonaws.com/gco/component-{index:03d}@sha256:"
            + hashlib.sha256(f"image-{index}".encode()).hexdigest()
        )
        for index in range(count)
    }


def _props_with_raw_size(props, size):
    """``props`` padded so the canonical execution input is exactly ``size`` bytes."""
    padded = {**props, "ImageReplacements": {"{{PAD}}": ""}}
    missing = size - len(_canonical(_execution_input(padded)).encode("utf-8"))
    padded["ImageReplacements"] = {
        "{{PAD}}": random.Random(size).randbytes(missing // 2 + 1).hex()[:missing]
    }
    assert len(_canonical(_execution_input(padded)).encode("utf-8")) == size
    return padded


def _reassemble(parameters, input_name):
    """Independent reader of the stored replay input: header, parts, digest, decode."""
    stored = parameters[input_name]
    match = re.fullmatch(r"chunked:v1:([1-9][0-9]*):([0-9a-f]{64})", stored)
    if match is not None:
        count, digest = int(match[1]), match[2]
        stored = "".join(
            parameters[f"{input_name}.{digest[:16]}.{index}"] for index in range(1, count + 1)
        )
        assert hashlib.sha256(stored.encode("ascii")).hexdigest() == digest
    return zlib.decompress(base64.b64decode(stored, validate=True)).decode("utf-8")


def _stored_parameters(ssm):
    """The final value of every parameter a MagicMock SSM client was asked to write."""
    return {call.kwargs["Name"]: call.kwargs["Value"] for call in ssm.put_parameter.call_args_list}


def _client_error(code, operation):
    return ClientError({"Error": {"Code": code, "Message": code}}, operation)


class _FakeSsm:
    """Dict-backed SSM client that keeps every call in ``events``, in order.

    ``read_error`` fails every GetParameter; ``delete_errors`` fails the
    DeleteParameter of the names it maps; ``put_errors`` fails the
    PutParameter of the names it maps.
    """

    def __init__(self, events=None, parameters=None):
        self.events = events if events is not None else []
        self.parameters = dict(parameters or {})
        self.writes = []
        self.read_error = None
        self.delete_errors = {}
        self.put_errors = {}

    def get_parameter(self, *, Name):
        self.events.append(("get", Name))
        if self.read_error is not None:
            raise self.read_error
        if Name not in self.parameters:
            raise _client_error("ParameterNotFound", "GetParameter")
        return {"Parameter": {"Name": Name, "Type": "String", "Value": self.parameters[Name]}}

    def put_parameter(self, **kwargs):
        name = kwargs["Name"]
        self.events.append(("put", name))
        if name in self.put_errors:
            raise self.put_errors[name]
        self.writes.append(kwargs)
        self.parameters[name] = kwargs["Value"]

    def delete_parameter(self, *, Name):
        self.events.append(("delete", Name))
        if Name in self.delete_errors:
            raise self.delete_errors[Name]
        if Name not in self.parameters:
            raise _client_error("ParameterNotFound", "DeleteParameter")
        del self.parameters[Name]


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

    def test_json_encoded_charts_reach_the_execution_with_their_types(self, orchestrator):
        """The stack sends Charts as one JSON string because CloudFormation turns
        every number and boolean in an object property into a string; decoded
        here, a Secret volume's ``optional`` and ``defaultMode`` stay typed."""
        handler, sfn = orchestrator
        sfn.start_execution.return_value = {"executionArn": _EXECUTION_ARN}
        charts = {
            "opencost": {
                "values": {
                    "extraVolumes": [
                        {
                            "name": "gco-tls",
                            "secret": {
                                "secretName": "opencost-tls",
                                "optional": True,
                                "defaultMode": 292,
                            },
                        }
                    ]
                }
            }
        }

        handler.on_event(
            {"RequestType": "Create", "ResourceProperties": self._props(Charts=json.dumps(charts))}
        )

        sent = json.loads(sfn.start_execution.call_args.kwargs["input"])
        assert sent["Charts"] == charts
        secret = sent["Charts"]["opencost"]["values"]["extraVolumes"][0]["secret"]
        assert secret["optional"] is True
        assert secret["defaultMode"] == 292

    @pytest.mark.parametrize(
        ("charts", "message"),
        [
            ("{not json", "Charts is not valid JSON"),
            ('["keda"]', "Charts must be a JSON object"),
            (["keda"], "Charts must be a JSON object"),
        ],
    )
    def test_malformed_charts_fail_before_anything_is_written(self, orchestrator, charts, message):
        handler, sfn = orchestrator
        ssm = MagicMock()

        with (
            patch.object(handler, "_ssm", return_value=ssm),
            pytest.raises(ValueError, match=message),
        ):
            handler.on_event(
                {"RequestType": "Create", "ResourceProperties": self._props(Charts=charts)}
            )

        handler._prepare_teardown_fence.assert_not_called()
        ssm.put_parameter.assert_not_called()
        sfn.start_execution.assert_not_called()

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

    def test_raw_input_over_the_step_functions_limit_fails_before_anything_is_written(
        self, orchestrator
    ):
        """StartExecution's 256 KiB is the input's real bound; the SSM copy is
        stored in parts, so its encoded size no longer refuses a deployment."""
        handler, sfn = orchestrator
        ssm = MagicMock()
        props = _props_with_raw_size(self._props(), 256 * 1024 + 1)
        with (
            patch.object(handler, "_ssm", return_value=ssm),
            pytest.raises(
                ValueError,
                match=r"is 262145 bytes; Step Functions accepts at most 262144 bytes",
            ),
        ):
            handler.on_event({"RequestType": "Update", "ResourceProperties": props})
        handler._prepare_teardown_fence.assert_not_called()
        assert ssm.mock_calls == []
        sfn.start_execution.assert_not_called()

    def test_raw_input_at_the_step_functions_limit_is_started(self, orchestrator):
        handler, sfn = orchestrator
        ssm = MagicMock()
        sfn.start_execution.return_value = {"executionArn": _EXECUTION_ARN}
        props = _props_with_raw_size(self._props(), 256 * 1024)

        with patch.object(handler, "_ssm", return_value=ssm):
            handler.on_event({"RequestType": "Update", "ResourceProperties": props})

        sent = sfn.start_execution.call_args.kwargs["input"]
        assert len(sent.encode("utf-8")) == 256 * 1024
        stored = _stored_parameters(ssm)
        assert _reassemble(stored, "/gco/addons/us-east-1/_input") == sent

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


class TestReplayInputParts:
    """The SSM replay copy of an input whose encoding exceeds one 8 KiB value."""

    _PARTS_NAME = re.compile(r"/gco/addons/us-east-1/_input\.[0-9a-f]{16}\.[1-9][0-9]*")

    def _props(self, **overrides):
        return TestOnEvent()._props(**overrides)

    def _run(self, orchestrator, ssm, props, *, request_id="request-123", events=None):
        handler, sfn = orchestrator

        def start_execution(**kwargs):
            if events is not None:
                events.append(("start", kwargs["stateMachineArn"]))
            return {"executionArn": _EXECUTION_ARN, "startDate": _START_DATE}

        sfn.start_execution.side_effect = start_execution
        with patch.object(handler, "_ssm", return_value=ssm):
            result = handler.on_event(
                {"RequestType": "Update", "RequestId": request_id, "ResourceProperties": props}
            )
        assert result["Data"]["ExecutionArn"] == _EXECUTION_ARN
        return sfn.start_execution.call_args.kwargs["input"]

    def _part_names(self, ssm):
        return sorted(name for name in ssm.parameters if self._PARTS_NAME.fullmatch(name))

    def test_an_input_too_large_for_one_parameter_is_stored_in_parts(self, orchestrator):
        """Regression: a live deploy (default charts plus Slurm and YuniKorn, PCS,
        HyperPod, EKS capabilities) rolled back with "Encoded convergence replay
        input is 8232 bytes; SSM Parameter Store supports at most 8192 bytes".
        Its 143 image replacements carry 64-hex digests that zlib can barely
        shrink. The input is now stored as parts written before their header,
        and the execution starts with the exact canonical JSON."""
        events = []
        ssm = _FakeSsm(events)
        props = self._props(ImageReplacements=_image_replacements(143))
        expected_json = _canonical(_execution_input(props))
        encoded = _encoded(expected_json)
        assert len(encoded) > 8192, "the fixture must reproduce the live over-limit shape"

        sent = self._run(orchestrator, ssm, props, events=events)

        assert sent == expected_json
        digest = hashlib.sha256(encoded.encode("ascii")).hexdigest()
        part_names = [f"{_INPUT_NAME}.{digest[:16]}.{index}" for index in (1, 2)]
        header = f"chunked:v1:2:{digest}"
        assert ssm.writes[:3] == [
            {
                "Name": name,
                "Value": value,
                "Type": "String",
                "Tier": "Intelligent-Tiering",
                "Overwrite": True,
            }
            for name, value in (
                (part_names[0], encoded[:8192]),
                (part_names[1], encoded[8192:]),
                (_INPUT_NAME, header),
            )
        ]
        # The previous value is read first; the parts precede their header,
        # which precedes the execution and its record.
        assert events == [
            ("get", _INPUT_NAME),
            ("put", part_names[0]),
            ("put", part_names[1]),
            ("put", _INPUT_NAME),
            ("start", _STATE_MACHINE_ARN),
            ("put", _EXECUTION_RECORD_NAME),
        ]
        # The header cannot be read as either older form.
        assert not header.startswith("{")
        with pytest.raises(binascii.Error):
            base64.b64decode(header, validate=True)
        assert _reassemble(ssm.parameters, _INPUT_NAME) == expected_json
        record = json.loads(ssm.parameters[_EXECUTION_RECORD_NAME])
        assert record["input_sha256"] == hashlib.sha256(expected_json.encode()).hexdigest()

    def test_an_input_that_fits_one_parameter_keeps_the_single_value_byte_for_byte(
        self, orchestrator
    ):
        events = []
        ssm = _FakeSsm(events)
        props = self._props(ImageReplacements=_image_replacements(20))

        sent = self._run(orchestrator, ssm, props)

        assert sorted(ssm.parameters) == [_EXECUTION_RECORD_NAME, _INPUT_NAME]
        assert ssm.parameters[_INPUT_NAME] == _encoded(sent)
        assert events == [
            ("get", _INPUT_NAME),
            ("put", _INPUT_NAME),
            ("put", _EXECUTION_RECORD_NAME),
        ]

    def test_an_encoding_of_exactly_one_value_is_not_split(self, orchestrator):
        handler, _ = orchestrator
        assert handler._replay_input_writes("/p/_input", "A" * 8192) == [("/p/_input", "A" * 8192)]

        encoded = "A" * 8192 + "B"
        digest = hashlib.sha256(encoded.encode()).hexdigest()
        assert handler._replay_input_writes("/p/_input", encoded) == [
            (f"/p/_input.{digest[:16]}.1", "A" * 8192),
            (f"/p/_input.{digest[:16]}.2", "B"),
            ("/p/_input", f"chunked:v1:2:{digest}"),
        ]

    def test_a_new_input_replaces_the_previous_parts_after_its_execution_is_recorded(
        self, orchestrator
    ):
        events = []
        ssm = _FakeSsm(events)
        self._run(orchestrator, ssm, self._props(ImageReplacements=_image_replacements(300)))
        previous_parts = self._part_names(ssm)
        assert len(previous_parts) == 3
        events.clear()

        newer = self._props(
            ImageReplacements=_image_replacements(143), DeploymentTimestamp="2026-07-19T00:00:00Z"
        )
        sent = self._run(orchestrator, ssm, newer, request_id="request-456", events=events)

        assert _reassemble(ssm.parameters, _INPUT_NAME) == sent
        current_parts = self._part_names(ssm)
        assert len(current_parts) == 2
        assert not set(current_parts) & set(previous_parts)
        record = events.index(("put", _EXECUTION_RECORD_NAME))
        assert events[record + 1 :] == [("delete", name) for name in previous_parts]

    def test_parts_are_deleted_when_the_new_input_fits_one_parameter(self, orchestrator):
        ssm = _FakeSsm()
        self._run(orchestrator, ssm, self._props(ImageReplacements=_image_replacements(143)))
        assert len(self._part_names(ssm)) == 2

        sent = self._run(orchestrator, ssm, self._props(), request_id="request-456")

        assert sorted(ssm.parameters) == [_EXECUTION_RECORD_NAME, _INPUT_NAME]
        assert ssm.parameters[_INPUT_NAME] == _encoded(sent)

    def test_a_retry_with_the_same_input_keeps_its_parts(self, orchestrator):
        events = []
        ssm = _FakeSsm(events)
        props = self._props(ImageReplacements=_image_replacements(143))
        first = self._run(orchestrator, ssm, props)
        parts = self._part_names(ssm)

        second = self._run(orchestrator, ssm, props)

        assert second == first
        assert self._part_names(ssm) == parts
        assert not [event for event in events if event[0] == "delete"]
        assert _reassemble(ssm.parameters, _INPUT_NAME) == first

    @pytest.mark.parametrize(
        "error",
        [_client_error("AccessDeniedException", "GetParameter"), RuntimeError("network down")],
    )
    def test_a_failed_read_of_the_previous_input_is_logged_and_skips_the_cleanup(
        self, orchestrator, caplog, error
    ):
        ssm = _FakeSsm()
        ssm.parameters[f"{_INPUT_NAME}.{'a' * 16}.1"] = "stale"
        ssm.parameters[_INPUT_NAME] = f"chunked:v1:1:{'a' * 64}"
        ssm.read_error = error

        with caplog.at_level(logging.WARNING):
            sent = self._run(orchestrator, ssm, self._props())

        assert f"Could not read the previous replay input {_INPUT_NAME}" in caplog.text
        assert ssm.parameters[_INPUT_NAME] == _encoded(sent)
        # Unknown previous value: nothing is deleted; teardown sweeps the part.
        assert f"{_INPUT_NAME}.{'a' * 16}.1" in ssm.parameters

    def test_a_missing_previous_input_is_not_logged(self, orchestrator, caplog):
        with caplog.at_level(logging.WARNING):
            self._run(orchestrator, _FakeSsm(), self._props())
        assert "previous replay input" not in caplog.text

    def test_stale_part_delete_failures_are_logged_and_never_fail_the_deployment(
        self, orchestrator, caplog
    ):
        ssm = _FakeSsm()
        stale = [f"{_INPUT_NAME}.{'b' * 16}.{index}" for index in (1, 2, 3)]
        # Part 1 is already gone; part 2 cannot be deleted; part 3 can.
        ssm.parameters.update({stale[1]: "x", stale[2]: "y"})
        ssm.parameters[_INPUT_NAME] = f"chunked:v1:3:{'b' * 64}"
        ssm.delete_errors[stale[1]] = _client_error("AccessDeniedException", "DeleteParameter")

        with caplog.at_level(logging.WARNING):
            self._run(orchestrator, ssm, self._props())

        assert [event[1] for event in ssm.events if event[0] == "delete"] == stale
        assert f"Could not delete the stale replay-input part {stale[1]}" in caplog.text
        assert stale[0] not in caplog.text
        assert stale[1] in ssm.parameters
        assert stale[2] not in ssm.parameters

    def test_a_write_that_fails_part_way_keeps_the_previous_input_whole(self, orchestrator):
        handler, sfn = orchestrator
        ssm = _FakeSsm()
        self._run(orchestrator, ssm, self._props(ImageReplacements=_image_replacements(143)))
        previous = dict(ssm.parameters)
        newer = self._props(
            ImageReplacements=_image_replacements(300), DeploymentTimestamp="2026-07-19T00:00:00Z"
        )
        encoded = _encoded(_canonical(_execution_input(newer)))
        digest = hashlib.sha256(encoded.encode("ascii")).hexdigest()
        ssm.put_errors[f"{_INPUT_NAME}.{digest[:16]}.2"] = RuntimeError("throttled")
        sfn.reset_mock()

        with (
            patch.object(handler, "_ssm", return_value=ssm),
            pytest.raises(RuntimeError, match="Could not persist the convergence replay input"),
        ):
            handler.on_event({"RequestType": "Update", "ResourceProperties": newer})

        sfn.start_execution.assert_not_called()
        assert ssm.parameters[_INPUT_NAME] == previous[_INPUT_NAME]
        assert {name: ssm.parameters[name] for name in previous} == previous
        assert _reassemble(ssm.parameters, _INPUT_NAME) == _reassemble(previous, _INPUT_NAME)
        assert not [event for event in ssm.events if event[0] == "delete"]

    @pytest.mark.parametrize(
        "value",
        [
            None,
            "",
            '{"raw":true}',
            "eJwrSS0uAQAEXQH+",
            "chunked:v1:0:" + "a" * 64,
            "chunked:v1:02:" + "a" * 64,
            "chunked:v1:2:" + "a" * 63,
            "chunked:v1:2:" + "A" * 64,
            "chunked:v2:2:" + "a" * 64,
            " chunked:v1:2:" + "a" * 64,
        ],
    )
    def test_only_a_well_formed_header_names_parts_to_delete(self, orchestrator, value):
        handler, _ = orchestrator
        assert handler._replay_input_part_names("/p/_input", value) == []

    def test_a_header_names_its_parts_in_order(self, orchestrator):
        handler, _ = orchestrator
        assert handler._replay_input_part_names("/p/_input", "chunked:v1:2:" + "c" * 64) == [
            "/p/_input.cccccccccccccccc.1",
            "/p/_input.cccccccccccccccc.2",
        ]


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
