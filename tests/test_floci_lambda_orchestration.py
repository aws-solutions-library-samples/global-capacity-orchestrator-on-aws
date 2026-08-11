"""Floci layer: control-plane Lambda handlers against real emulated services.

Production handler modules run unmodified — loaded via
``tests._lambda_imports.load_lambda_module`` with the session environment
pointing every boto3 client at the emulator — and every assertion is about
real wire-level state (secret version stages, Step Functions executions,
SSM parameters, CloudFormation stack outputs):

* ``lambda/secret-rotation`` — the full four-step Secrets Manager rotation
  protocol, driven exactly as Secrets Manager would drive it.
* ``lambda/helm-orchestrator`` — the fire-and-forget convergence provider:
  execution start, SSM replay-input/execution-identity persistence,
  retry adoption via ``ExecutionAlreadyExists``, and the teardown fence.
* ``lambda/helm-installer/teardown_provider.py`` — ordered-teardown start,
  install-execution draining, and ``is_complete`` terminal-status mapping.
* ``lambda/cross-region-aggregator`` — regional API discovery through real
  CloudFormation stacks, including fail-closed and bounded-stale behavior.

EKS-dependent paths (kubeconfig construction, in-cluster HTTP) stay in the
unit suites: Floci clusters never reach ACTIVE (documented in
docs/FLOCI_TESTING.md), so those seams cannot be exercised here honestly.
"""

from __future__ import annotations

import base64
import hashlib
import json
import time
import uuid
import zlib

import boto3
import pytest

from tests._floci import floci_test_markers, unique_name
from tests._lambda_imports import load_lambda_module

pytestmark = floci_test_markers()

_PASS_DEFINITION = json.dumps(
    {"StartAt": "Done", "States": {"Done": {"Type": "Pass", "End": True}}}
)
_WAIT_DEFINITION = json.dumps(
    {
        "StartAt": "Hold",
        "States": {
            "Hold": {"Type": "Wait", "Seconds": 300, "Next": "Done"},
            "Done": {"Type": "Succeed"},
        },
    }
)
_FAIL_DEFINITION = json.dumps(
    {
        "StartAt": "Boom",
        "States": {"Boom": {"Type": "Fail", "Error": "Boom", "Cause": "teardown boom"}},
    }
)


@pytest.fixture()
def sfn(verified_floci_endpoint: str):
    return boto3.client("stepfunctions")


@pytest.fixture()
def ssm(verified_floci_endpoint: str):
    return boto3.client("ssm")


def _state_machine(sfn, floci_account: str, definition: str) -> str:
    return sfn.create_state_machine(
        name=unique_name("gco-floci-sm"),
        definition=definition,
        roleArn=f"arn:aws:iam::{floci_account}:role/floci-test",
    )["stateMachineArn"]


def _wait_terminal(sfn, execution_arn: str, timeout: float = 15.0) -> str:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        status = sfn.describe_execution(executionArn=execution_arn)["status"]
        if status != "RUNNING":
            return str(status)
        time.sleep(0.2)
    return "RUNNING"


# ---------------------------------------------------------------------------
# secret-rotation
# ---------------------------------------------------------------------------


class TestSecretRotationLifecycle:
    """The four-step rotation protocol against a real emulator secret."""

    @pytest.fixture()
    def secretsmanager(self, verified_floci_endpoint: str):
        return boto3.client("secretsmanager")

    @pytest.fixture()
    def rotation(self, secretsmanager):
        handler = load_lambda_module("secret-rotation")
        secret_arn = secretsmanager.create_secret(
            Name=unique_name("gco-signing-key"),
            SecretString=json.dumps({"description": "seed", "token": "seed-token"}),
        )["ARN"]
        return handler, secret_arn

    def _step(self, handler, secret_arn: str, token: str, step: str) -> None:
        handler.lambda_handler(
            {"SecretId": secret_arn, "ClientRequestToken": token, "Step": step},
            None,
        )

    def test_full_rotation_moves_pending_to_current(self, secretsmanager, rotation):
        handler, secret_arn = rotation
        token = str(uuid.uuid4())

        self._step(handler, secret_arn, token, "createSecret")
        pending = secretsmanager.get_secret_value(
            SecretId=secret_arn, VersionId=token, VersionStage="AWSPENDING"
        )
        pending_token = json.loads(pending["SecretString"])["token"]
        assert len(pending_token) == handler.TOKEN_LENGTH, (
            "createSecret must stage a token of the configured length as AWSPENDING"
        )

        self._step(handler, secret_arn, token, "setSecret")
        self._step(handler, secret_arn, token, "testSecret")
        self._step(handler, secret_arn, token, "finishSecret")

        current = secretsmanager.get_secret_value(SecretId=secret_arn)
        assert json.loads(current["SecretString"])["token"] == pending_token, (
            "finishSecret must promote the staged AWSPENDING value to AWSCURRENT"
        )
        stages = secretsmanager.describe_secret(SecretId=secret_arn)["VersionIdsToStages"]
        assert "AWSCURRENT" in stages.get(token, []), (
            f"the rotation token version must now carry AWSCURRENT; got {stages}"
        )

    def test_create_secret_is_idempotent_per_token(self, secretsmanager, rotation):
        handler, secret_arn = rotation
        token = str(uuid.uuid4())

        self._step(handler, secret_arn, token, "createSecret")
        first = secretsmanager.get_secret_value(
            SecretId=secret_arn, VersionId=token, VersionStage="AWSPENDING"
        )["SecretString"]
        self._step(handler, secret_arn, token, "createSecret")
        second = secretsmanager.get_secret_value(
            SecretId=secret_arn, VersionId=token, VersionStage="AWSPENDING"
        )["SecretString"]
        assert first == second, (
            "a retried createSecret with the same ClientRequestToken must not regenerate the key"
        )

    def test_finish_secret_is_idempotent_once_current(self, secretsmanager, rotation):
        handler, secret_arn = rotation
        token = str(uuid.uuid4())
        for step in ("createSecret", "testSecret", "finishSecret"):
            self._step(handler, secret_arn, token, step)
        before = secretsmanager.get_secret_value(SecretId=secret_arn)["SecretString"]
        # Secrets Manager retries finishSecret; re-running must be a no-op.
        self._step(handler, secret_arn, token, "finishSecret")
        after = secretsmanager.get_secret_value(SecretId=secret_arn)["SecretString"]
        assert before == after

    def test_invalid_step_is_rejected(self, rotation):
        handler, secret_arn = rotation
        with pytest.raises(ValueError, match="Invalid rotation step"):
            self._step(handler, secret_arn, str(uuid.uuid4()), "explodeSecret")


# ---------------------------------------------------------------------------
# helm-orchestrator (fire-and-forget convergence provider)
# ---------------------------------------------------------------------------


def _orchestrator_event(project: str, request_id: str, request_type: str = "Create") -> dict:
    return {
        "RequestType": request_type,
        "RequestId": request_id,
        "ResourceProperties": {
            "ClusterName": f"{project}-us-east-1",
            "Region": "us-east-1",
            "RegistryRegion": "us-east-1",
            "ProjectName": project,
            "DeploymentTimestamp": "2026-08-11T00:00:00Z",
            "EnabledCharts": ["keda"],
            "Charts": {"keda": {"namespace": "keda"}},
            "ImageReplacements": {"{{QUEUE_PROCESSOR_IMAGE}}": "registry/queue:1"},
        },
    }


class TestHelmOrchestratorProvider:
    @pytest.fixture()
    def orchestrator(self, sfn, ssm, floci_account, monkeypatch):
        state_machine_arn = _state_machine(sfn, floci_account, _WAIT_DEFINITION)
        monkeypatch.setenv("STATE_MACHINE_ARN", state_machine_arn)
        handler = load_lambda_module("helm-orchestrator")
        return handler, state_machine_arn

    def test_create_starts_execution_and_persists_replay_identity(self, orchestrator, sfn, ssm):
        handler, _ = orchestrator
        project = unique_name("gcofloci")
        result = handler.on_event(_orchestrator_event(project, str(uuid.uuid4())))

        execution_arn = result["Data"]["ExecutionArn"]
        assert sfn.describe_execution(executionArn=execution_arn)["status"] == "RUNNING", (
            "the provider is fire-and-forget: the execution must be started and left running"
        )

        encoded = ssm.get_parameter(Name=f"/{project}/addons/us-east-1/_input")["Parameter"][
            "Value"
        ]
        replay_json = zlib.decompress(base64.b64decode(encoded)).decode("utf-8")
        replay = json.loads(replay_json)
        assert replay["ImageReplacements"] == {"{{QUEUE_PROCESSOR_IMAGE}}": "registry/queue:1"}, (
            "the zlib+base64 replay input must round-trip the raw {{PLACEHOLDER}} tokens "
            "SSM would otherwise reject"
        )

        metadata = json.loads(
            ssm.get_parameter(Name=f"/{project}/addons/us-east-1/_execution")["Parameter"]["Value"]
        )
        assert metadata["execution_arn"] == execution_arn
        assert metadata["input_sha256"] == hashlib.sha256(replay_json.encode()).hexdigest(), (
            "the persisted digest must be computed over the raw canonical JSON, "
            "not the encoded form"
        )

    def test_provider_retry_adopts_identical_running_execution(self, orchestrator):
        handler, _ = orchestrator
        project = unique_name("gcofloci")
        request_id = str(uuid.uuid4())

        first = handler.on_event(_orchestrator_event(project, request_id))
        second = handler.on_event(_orchestrator_event(project, request_id))
        assert first["ExecutionArn"] == second["ExecutionArn"], (
            "a CloudFormation provider retry with identical input must adopt the running "
            "execution instead of starting a duplicate"
        )

    def test_provider_retry_with_different_input_is_refused(self, orchestrator):
        handler, _ = orchestrator
        project = unique_name("gcofloci")
        request_id = str(uuid.uuid4())

        handler.on_event(_orchestrator_event(project, request_id))
        drifted = _orchestrator_event(project, request_id)
        drifted["ResourceProperties"]["EnabledCharts"] = ["keda", "volcano"]
        with pytest.raises(RuntimeError, match="non-identical input"):
            handler.on_event(drifted)

    def test_active_teardown_fence_blocks_update_convergence(self, orchestrator, ssm):
        handler, _ = orchestrator
        project = unique_name("gcofloci")
        fence = f"/{project}/addons/us-east-1/_teardown"
        ssm.put_parameter(Name=fence, Value="helm-delete-abc", Type="String")

        with pytest.raises(RuntimeError, match="teardown fence is active"):
            handler.on_event(_orchestrator_event(project, str(uuid.uuid4()), "Update"))

    def test_create_clears_a_stale_teardown_fence(self, orchestrator, ssm):
        handler, _ = orchestrator
        project = unique_name("gcofloci")
        fence = f"/{project}/addons/us-east-1/_teardown"
        ssm.put_parameter(Name=fence, Value="helm-delete-stale", Type="String")

        handler.on_event(_orchestrator_event(project, str(uuid.uuid4()), "Create"))
        with pytest.raises(ssm.exceptions.ParameterNotFound):
            ssm.get_parameter(Name=fence)

    def test_delete_is_a_no_op(self, orchestrator, sfn):
        handler, state_machine_arn = orchestrator
        result = handler.on_event(
            {"RequestType": "Delete", "PhysicalResourceId": "helm-install-charts"}
        )
        assert result == {"PhysicalResourceId": "helm-install-charts"}
        executions = sfn.list_executions(stateMachineArn=state_machine_arn)["executions"]
        assert executions == [], "Delete must not start any execution"


# ---------------------------------------------------------------------------
# helm-installer teardown provider
# ---------------------------------------------------------------------------


def _teardown_event(project: str, *, request_type: str = "Delete") -> dict:
    return {
        "RequestType": request_type,
        "StackId": f"arn:aws:cloudformation:us-east-1:000000000000:stack/{project}/1",
        "RequestId": str(uuid.uuid4()),
        "LogicalResourceId": "HelmTeardown",
        "ResourceProperties": {
            "ClusterName": f"{project}-us-east-1",
            "Region": "us-east-1",
            "RegistryRegion": "us-east-1",
            "ProjectName": project,
            "EnabledCharts": ["keda"],
            "Charts": {"keda": {"namespace": "keda"}},
        },
    }


class TestHelmTeardownProvider:
    @pytest.fixture()
    def provider(self, sfn, ssm, floci_account, monkeypatch):
        def _build(teardown_definition: str):
            teardown_arn = _state_machine(sfn, floci_account, teardown_definition)
            install_arn = _state_machine(sfn, floci_account, _WAIT_DEFINITION)
            monkeypatch.setenv("TEARDOWN_STATE_MACHINE_ARN", teardown_arn)
            monkeypatch.setenv("INSTALL_STATE_MACHINE_ARN", install_arn)
            handler = load_lambda_module("helm-installer", module_name="teardown_provider")
            return handler, teardown_arn, install_arn

        return _build

    def test_create_and_update_are_no_ops(self, provider, sfn):
        handler, teardown_arn, _ = provider(_PASS_DEFINITION)
        for request_type in ("Create", "Update"):
            result = handler.on_event(
                _teardown_event(unique_name("gco"), request_type=request_type)
            )
            assert result["PhysicalResourceId"] == "helm-teardown"
        assert sfn.list_executions(stateMachineArn=teardown_arn)["executions"] == []

    def test_delete_fences_stops_installs_and_starts_ordered_teardown(self, provider, sfn, ssm):
        handler, teardown_arn, install_arn = provider(_PASS_DEFINITION)
        project = unique_name("gcofloci")

        # A convergence execution is still running when teardown begins.
        running = sfn.start_execution(stateMachineArn=install_arn, input="{}")["executionArn"]

        event = _teardown_event(project)
        result = handler.on_event(event)
        assert result["PhysicalResourceId"] == "helm-teardown"

        fence = ssm.get_parameter(Name=f"/{project}/addons/us-east-1/_teardown")["Parameter"]
        assert fence["Value"] == handler._execution_name(event), (
            "the fence parameter must carry the teardown execution name so a late "
            "convergence attempt can be attributed"
        )
        assert _wait_terminal(sfn, running) != "RUNNING", (
            "on_event must stop visible running install executions before teardown starts"
        )
        expected_arn = handler._execution_arn(teardown_arn, handler._execution_name(event))
        assert sfn.describe_execution(executionArn=expected_arn)["status"] in {
            "RUNNING",
            "SUCCEEDED",
        }

    def test_delete_retry_reuses_the_deterministic_execution(self, provider, sfn):
        handler, teardown_arn, _ = provider(_WAIT_DEFINITION)
        event = _teardown_event(unique_name("gcofloci"))
        handler.on_event(event)
        # A provider retry replays the same Delete; ExecutionAlreadyExists is
        # suppressed and the original execution keeps running.
        handler.on_event(event)
        executions = sfn.list_executions(stateMachineArn=teardown_arn)["executions"]
        assert len(executions) == 1

    def test_is_complete_maps_terminal_states(self, provider, sfn):
        handler, teardown_arn, _ = provider(_PASS_DEFINITION)
        event = _teardown_event(unique_name("gcofloci"))
        handler.on_event(event)
        expected_arn = handler._execution_arn(teardown_arn, handler._execution_name(event))
        assert _wait_terminal(sfn, expected_arn) == "SUCCEEDED"
        assert handler.is_complete(event) == {"IsComplete": True}

    def test_is_complete_reports_running_and_raises_on_failure(self, provider, sfn):
        handler, _, _ = provider(_WAIT_DEFINITION)
        event = _teardown_event(unique_name("gcofloci"))
        handler.on_event(event)
        assert handler.is_complete(event) == {"IsComplete": False}

        failing_handler, failing_arn, _ = provider(_FAIL_DEFINITION)
        failing_event = _teardown_event(unique_name("gcofloci"))
        failing_handler.on_event(failing_event)
        expected_arn = failing_handler._execution_arn(
            failing_arn, failing_handler._execution_name(failing_event)
        )
        assert _wait_terminal(sfn, expected_arn) == "FAILED"
        with pytest.raises(RuntimeError, match="Helm teardown execution FAILED"):
            failing_handler.is_complete(failing_event)

    def test_non_delete_is_complete_short_circuits(self, provider):
        handler, _, _ = provider(_PASS_DEFINITION)
        event = _teardown_event(unique_name("gcofloci"), request_type="Update")
        assert handler.is_complete(event) == {"IsComplete": True}

    def test_drain_stops_every_visible_install_execution(self, provider, sfn):
        handler, _, install_arn = provider(_PASS_DEFINITION)
        first = sfn.start_execution(stateMachineArn=install_arn, input="{}")["executionArn"]
        second = sfn.start_execution(stateMachineArn=install_arn, input="{}")["executionArn"]

        result = handler.drain_install_executions({})
        assert result == {"StoppedExecutions": 2}
        for arn in (first, second):
            assert _wait_terminal(sfn, arn) != "RUNNING"

        # The emulator's ListExecutions keeps returning stopped executions
        # under statusFilter=RUNNING (documented in docs/FLOCI_TESTING.md), so
        # the drain loop's eventually-reports-zero contract stays covered by
        # the unit suite. What this proves at the wire level is the important
        # half: re-draining already-stopped executions is safe and non-raising
        # (StopExecution on a terminal execution is tolerated), which is what
        # the workflow's unconditional re-drain relies on.
        redrain = handler.drain_install_executions({})
        assert redrain["StoppedExecutions"] >= 0


# ---------------------------------------------------------------------------
# cross-region-aggregator discovery
# ---------------------------------------------------------------------------

_REGIONAL_API_URL = "https://abc123defg.execute-api.us-east-1.amazonaws.com/prod"


def _create_regional_api_stack(cfn, project: str, *, endpoint: str = _REGIONAL_API_URL) -> str:
    stack_name = f"{project}-regional-api-us-east-1"
    cfn.create_stack(
        StackName=stack_name,
        TemplateBody=json.dumps(
            {
                "Resources": {
                    "Marker": {
                        "Type": "AWS::SNS::Topic",
                        "Properties": {"TopicName": f"{project}-marker"},
                    }
                },
                "Outputs": {"RegionalApiEndpoint": {"Value": endpoint}},
            }
        ),
    )
    cfn.get_waiter("stack_create_complete").wait(StackName=stack_name)
    return stack_name


class TestCrossRegionAggregatorDiscovery:
    @pytest.fixture()
    def cfn(self, verified_floci_endpoint: str):
        return boto3.client("cloudformation")

    @pytest.fixture()
    def aggregator(self, monkeypatch):
        def _build(project: str, regions: list[str]):
            monkeypatch.setenv("PROJECT_NAME", project)
            monkeypatch.setenv("TARGET_REGIONS", json.dumps(regions))
            monkeypatch.setenv("AWS_URL_SUFFIX", "amazonaws.com")
            return load_lambda_module("cross-region-aggregator")

        return _build

    def test_discovers_endpoint_from_real_stack_output(self, cfn, aggregator):
        project = unique_name("gcoxr")
        _create_regional_api_stack(cfn, project)
        handler = aggregator(project, ["us-east-1"])
        assert handler.get_regional_endpoints() == {"us-east-1": _REGIONAL_API_URL}

    def test_discovery_fails_closed_when_a_bridge_stack_is_missing(self, cfn, aggregator):
        project = unique_name("gcoxr")
        _create_regional_api_stack(cfn, project)
        handler = aggregator(project, ["us-east-1", "us-east-2"])
        with pytest.raises(RuntimeError, match="regional API bridges are unavailable"):
            handler.get_regional_endpoints()

    def test_discovery_rejects_a_non_execute_api_output(self, cfn, aggregator):
        project = unique_name("gcoxr")
        _create_regional_api_stack(
            cfn, project, endpoint="https://internal-alb.us-east-1.elb.amazonaws.com/prod"
        )
        handler = aggregator(project, ["us-east-1"])
        with pytest.raises(RuntimeError, match="regional API bridges are unavailable"):
            handler.get_regional_endpoints()

    def test_bounded_stale_cache_survives_a_discovery_outage(self, cfn, aggregator):
        project = unique_name("gcoxr")
        stack_name = _create_regional_api_stack(cfn, project)
        handler = aggregator(project, ["us-east-1"])
        first = handler.get_regional_endpoints()

        cfn.delete_stack(StackName=stack_name)
        cfn.get_waiter("stack_delete_complete").wait(StackName=stack_name)
        # Age the cache past the fresh TTL but inside the stale bound.
        handler._endpoints_cache_time -= handler._ENDPOINTS_CACHE_TTL + 1

        assert handler.get_regional_endpoints() == first, (
            "within the stale bound a discovery outage must fall back to the last "
            "known-good endpoints instead of taking the aggregate API down"
        )

    def test_lambda_handler_routes_and_shields(self, aggregator, monkeypatch):
        handler = aggregator(unique_name("gcoxr"), ["us-east-1"])
        not_found = handler.lambda_handler({"httpMethod": "GET", "path": "/nope"}, None)
        assert not_found["statusCode"] == 404

        monkeypatch.delenv("TARGET_REGIONS")
        unavailable = handler.lambda_handler(
            {"httpMethod": "GET", "path": "/api/v1/global/health"}, None
        )
        assert unavailable["statusCode"] == 503, (
            "an unconfigured discovery layer must surface 503, never a traceback"
        )
