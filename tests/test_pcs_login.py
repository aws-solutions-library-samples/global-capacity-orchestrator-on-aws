"""Tests for ``cli/pcs_login.py``, the PCS login node behind ``gco slurm shell`` / ``output``.

Real botocore clients run behind ``Stubber`` so every AWS call and its exact
parameters are asserted without a network: the regional stack's outputs
(``describe-stacks``), the login instance found through PCS's
``aws:pcs:compute-node-group-id`` tag, the ``aws ssm start-session`` argv for
an interactive shell and ``tail -f``, and the ``SendCommand`` /
``GetCommandInvocation`` read with its eventual-consistency retry, failure,
timeout and 24,000-character truncation cases.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Any

import boto3
import pytest
from botocore.stub import ANY, Stubber

from cli import pcs_login
from cli.pcs_login import CapturedOutput, LoginNode, PcsLoginError

_REGION = "us-east-1"
_STACK = "gco-us-east-1"
_CLUSTER_ID = "pcs_0a1b2c3d4e"
_GROUP_ID = "pcs_login0001"
_INSTANCE = "i-0abcdef1234567890"
_COMMAND_ID = "0f2a5c1e-9b3d-4e7a-8c6f-1d2e3f4a5b6c"
NODE = LoginNode(
    region=_REGION, cluster_id=_CLUSTER_ID, node_group_id=_GROUP_ID, instance_id=_INSTANCE
)


class _Clients:
    """Stubbed CloudFormation, EC2 and SSM clients behind one factory."""

    def __init__(self) -> None:
        kwargs = {
            "region_name": _REGION,
            "aws_access_key_id": "testing",
            "aws_secret_access_key": "testing",  # nosec B106  # fake credentials for stubbed clients
        }
        self.cfn = boto3.client("cloudformation", **kwargs)
        self.ec2 = boto3.client("ec2", **kwargs)
        self.ssm = boto3.client("ssm", **kwargs)
        self.stubs = {
            "cloudformation": Stubber(self.cfn),
            "ec2": Stubber(self.ec2),
            "ssm": Stubber(self.ssm),
        }
        self.requests: list[tuple[str, str]] = []

    def factory(self, service: str, *, region_name: str) -> Any:
        self.requests.append((service, region_name))
        return {"cloudformation": self.cfn, "ec2": self.ec2, "ssm": self.ssm}[service]

    def expect_outputs(self, outputs: dict[str, str] | None) -> None:
        if outputs is None:
            self.stubs["cloudformation"].add_client_error(
                "describe_stacks",
                service_error_code="ValidationError",
                service_message=f"Stack with id {_STACK} does not exist",
                expected_params={"StackName": _STACK},
            )
            return
        self.stubs["cloudformation"].add_response(
            "describe_stacks",
            {
                "Stacks": [
                    {
                        "StackName": _STACK,
                        "CreationTime": datetime(2026, 1, 1, tzinfo=UTC),
                        "StackStatus": "UPDATE_COMPLETE",
                        "Outputs": [
                            {"OutputKey": key, "OutputValue": value}
                            for key, value in outputs.items()
                        ],
                    }
                ]
            },
            {"StackName": _STACK},
        )

    def expect_instances(self, instances: list[dict[str, Any]]) -> None:
        self.stubs["ec2"].add_response(
            "describe_instances",
            {"Reservations": [{"Instances": instances}] if instances else []},
            {
                "Filters": [
                    {"Name": "tag:aws:pcs:compute-node-group-id", "Values": [_GROUP_ID]},
                    {"Name": "instance-state-name", "Values": ["running"]},
                ]
            },
        )


@pytest.fixture
def clients() -> Iterator[_Clients]:
    stubbed = _Clients()
    for stub in stubbed.stubs.values():
        stub.activate()
    yield stubbed
    for stub in stubbed.stubs.values():
        stub.assert_no_pending_responses()
        stub.deactivate()


def _instance(instance_id: str, launched: str) -> dict[str, Any]:
    return {
        "InstanceId": instance_id,
        "State": {"Name": "running"},
        "LaunchTime": datetime.fromisoformat(launched).replace(tzinfo=UTC),
    }


MANAGED_OUTPUTS = {
    "PcsClusterId": _CLUSTER_ID,
    "PcsMode": "managed",
    "PcsLoginNodeGroupId": _GROUP_ID,
    "ClusterName": "gco-us-east-1",
}


class TestStackOutputs:
    def test_outputs_by_key(self, clients: _Clients) -> None:
        clients.expect_outputs(MANAGED_OUTPUTS)
        assert pcs_login.stack_outputs(_REGION, _STACK, client_factory=clients.factory) == (
            MANAGED_OUTPUTS
        )
        assert clients.requests == [("cloudformation", _REGION)]

    def test_a_missing_stack_reads_as_no_outputs(self, clients: _Clients) -> None:
        clients.expect_outputs(None)
        assert pcs_login.stack_outputs(_REGION, _STACK, client_factory=clients.factory) == {}

    def test_other_failures_are_reported(self, clients: _Clients) -> None:
        clients.stubs["cloudformation"].add_client_error(
            "describe_stacks",
            service_error_code="AccessDenied",
            service_message="not allowed",
            expected_params={"StackName": _STACK},
        )
        with pytest.raises(
            PcsLoginError, match=r"could not read stack gco-us-east-1.*AccessDenied"
        ):
            pcs_login.stack_outputs(_REGION, _STACK, client_factory=clients.factory)

    def test_an_empty_answer_reads_as_no_outputs(self, clients: _Clients) -> None:
        clients.stubs["cloudformation"].add_response(
            "describe_stacks", {"Stacks": []}, {"StackName": _STACK}
        )
        assert pcs_login.stack_outputs(_REGION, _STACK, client_factory=clients.factory) == {}


class TestFindLoginNode:
    def test_finds_the_running_login_instance(self, clients: _Clients) -> None:
        clients.expect_outputs(MANAGED_OUTPUTS)
        clients.expect_instances([_instance(_INSTANCE, "2026-10-01T10:00:00")])
        node = pcs_login.find_login_node(_REGION, _STACK, client_factory=clients.factory)
        assert node == NODE
        assert clients.requests == [("cloudformation", _REGION), ("ec2", _REGION)]

    def test_the_newest_instance_wins_while_pcs_replaces_the_node(self, clients: _Clients) -> None:
        clients.expect_outputs(MANAGED_OUTPUTS)
        clients.expect_instances(
            [
                _instance("i-00000000000000001", "2026-09-01T10:00:00"),
                _instance("i-00000000000000002", "2026-10-01T10:00:00"),
            ]
        )
        node = pcs_login.find_login_node(_REGION, _STACK, client_factory=clients.factory)
        assert node.instance_id == "i-00000000000000002"

    def test_pcs_not_deployed(self, clients: _Clients) -> None:
        clients.expect_outputs({"ClusterName": "gco-us-east-1"})
        with pytest.raises(
            PcsLoginError, match=r"AWS PCS is not deployed in us-east-1.*gco stacks pcs enable"
        ):
            pcs_login.find_login_node(_REGION, _STACK, client_factory=clients.factory)

    def test_missing_stack(self, clients: _Clients) -> None:
        clients.expect_outputs(None)
        with pytest.raises(PcsLoginError, match=r"AWS PCS is not deployed in us-east-1"):
            pcs_login.find_login_node(_REGION, _STACK, client_factory=clients.factory)

    def test_existing_mode_has_no_managed_login_node(self, clients: _Clients) -> None:
        clients.expect_outputs({"PcsClusterId": _CLUSTER_ID, "PcsMode": "existing"})
        with pytest.raises(
            PcsLoginError, match=r"operator-run \(pcs\.mode existing\).*aws ssm start-session"
        ):
            pcs_login.find_login_node(_REGION, _STACK, client_factory=clients.factory)

    def test_login_node_disabled(self, clients: _Clients) -> None:
        clients.expect_outputs({"PcsClusterId": _CLUSTER_ID, "PcsMode": "managed"})
        with pytest.raises(
            PcsLoginError, match=r"has no login node \(pcs\.managed\.login\.enabled"
        ):
            pcs_login.find_login_node(_REGION, _STACK, client_factory=clients.factory)

    def test_no_running_instance_yet(self, clients: _Clients) -> None:
        clients.expect_outputs(MANAGED_OUTPUTS)
        clients.expect_instances([])
        with pytest.raises(PcsLoginError, match=r"is not running yet.*pcs_login0001"):
            pcs_login.find_login_node(_REGION, _STACK, client_factory=clients.factory)

    def test_an_odd_instance_id_is_refused(self, clients: _Clients) -> None:
        clients.expect_outputs(MANAGED_OUTPUTS)
        clients.expect_instances([_instance("mi-0123456789abcdef0", "2026-10-01T10:00:00")])
        with pytest.raises(PcsLoginError, match=r"unexpected login instance id 'mi-"):
            pcs_login.find_login_node(_REGION, _STACK, client_factory=clients.factory)

    def test_an_invalid_region_fails_before_any_call(self, clients: _Clients) -> None:
        with pytest.raises(PcsLoginError, match=r"invalid AWS region 'nowhere'"):
            pcs_login.find_login_node("nowhere", _STACK, client_factory=clients.factory)
        assert clients.requests == []


class TestSessionCommands:
    def test_shell_as_the_identity(self) -> None:
        assert pcs_login.shell_command(NODE, user="ec2-user") == [
            "aws",
            "ssm",
            "start-session",
            "--target",
            _INSTANCE,
            "--region",
            _REGION,
            "--document-name",
            "AWS-StartInteractiveCommand",
            "--parameters",
            json.dumps({"command": ["sudo -iu ec2-user"]}),
        ]

    @pytest.mark.parametrize("user", ["", "Bad User", "root;id", "1000", "a" * 33])
    def test_a_non_posix_user_is_refused(self, user: str) -> None:
        with pytest.raises(PcsLoginError, match="not a POSIX user name"):
            pcs_login.shell_command(NODE, user=user)

    def test_follow_quotes_the_path(self) -> None:
        cmd = pcs_login.follow_command(NODE, "/home/ec2-user/slurm-42.out")
        assert cmd[-3:] == [
            "AWS-StartInteractiveCommand",
            "--parameters",
            json.dumps({"command": ["sudo tail -f /home/ec2-user/slurm-42.out"]}),
        ]
        odd = pcs_login.follow_command(NODE, "/home/ec2-user/my file.out")
        assert json.loads(odd[-1]) == {"command": ["sudo tail -f '/home/ec2-user/my file.out'"]}


class TestReadFile:
    def _expect_send(self, clients: _Clients, command: str) -> None:
        clients.stubs["ssm"].add_response(
            "send_command",
            {"Command": {"CommandId": _COMMAND_ID}},
            {
                "InstanceIds": [_INSTANCE],
                "DocumentName": "AWS-RunShellScript",
                "Parameters": {"commands": [command]},
                "TimeoutSeconds": ANY,
                "Comment": "gco slurm output (PCS login node)",
            },
        )

    def _expect_invocation(self, clients: _Clients, status: str, **fields: Any) -> None:
        clients.stubs["ssm"].add_response(
            "get_command_invocation",
            {"Status": status, **fields},
            {"CommandId": _COMMAND_ID, "InstanceId": _INSTANCE},
        )

    def test_reads_stdout_after_the_invocation_appears(self, clients: _Clients) -> None:
        self._expect_send(clients, "cat /home/ec2-user/slurm-42.out")
        # The first poll races SendCommand's registration, the second is
        # still running, the third has finished.
        clients.stubs["ssm"].add_client_error(
            "get_command_invocation",
            service_error_code="InvocationDoesNotExist",
            expected_params={"CommandId": _COMMAND_ID, "InstanceId": _INSTANCE},
        )
        self._expect_invocation(clients, "InProgress")
        self._expect_invocation(clients, "Success", StandardOutputContent="hello\n")
        sleeps: list[float] = []
        clock = iter([0.0, 1.0, 2.0, 3.0])
        captured = pcs_login.read_file(
            NODE,
            ["cat", "/home/ec2-user/slurm-42.out"],
            client_factory=clients.factory,
            sleep=sleeps.append,
            clock=lambda: next(clock),
            poll_interval=0.5,
        )
        assert captured == CapturedOutput(text="hello\n", truncated=False)
        assert sleeps == [0.5, 0.5]
        assert clients.requests == [("ssm", _REGION)]

    def test_truncated_output_is_flagged_and_cut_at_the_marker(self, clients: _Clients) -> None:
        self._expect_send(clients, "tail -n 50 /home/ec2-user/slurm-42.out")
        self._expect_invocation(
            clients, "Success", StandardOutputContent="first part\n---Output truncated---"
        )
        captured = pcs_login.read_file(
            NODE,
            ["tail", "-n", "50", "/home/ec2-user/slurm-42.out"],
            client_factory=clients.factory,
            sleep=lambda _s: None,
            clock=lambda: 0.0,
        )
        assert captured == CapturedOutput(text="first part\n", truncated=True)

    def test_a_failed_command_carries_its_stderr(self, clients: _Clients) -> None:
        self._expect_send(clients, "cat /home/ec2-user/missing.out")
        self._expect_invocation(
            clients,
            "Failed",
            StandardErrorContent="cat: /home/ec2-user/missing.out: No such file or directory\n",
        )
        with pytest.raises(
            PcsLoginError,
            match=r"cat /home/ec2-user/missing.out failed on the PCS login node i-0abc.*\(Failed\): "
            r"cat: .*No such file",
        ):
            pcs_login.read_file(
                NODE,
                ["cat", "/home/ec2-user/missing.out"],
                client_factory=clients.factory,
                sleep=lambda _s: None,
                clock=lambda: 0.0,
            )

    def test_a_failed_command_without_stderr(self, clients: _Clients) -> None:
        self._expect_send(clients, "cat /x")
        self._expect_invocation(clients, "TimedOut")
        with pytest.raises(
            PcsLoginError, match=r"cat /x failed on the PCS login node .*\(TimedOut\)$"
        ):
            pcs_login.read_file(
                NODE, ["cat", "/x"], client_factory=clients.factory, sleep=lambda _s: None
            )

    def test_times_out_while_the_command_runs(self, clients: _Clients) -> None:
        self._expect_send(clients, "cat /x")
        self._expect_invocation(clients, "InProgress")
        self._expect_invocation(clients, "InProgress")
        clock = iter([0.0, 5.0, 11.0])
        with pytest.raises(PcsLoginError, match=r"timed out after 10s waiting for cat .*0f2a5c1e"):
            pcs_login.read_file(
                NODE,
                ["cat", "/x"],
                client_factory=clients.factory,
                timeout=10.0,
                sleep=lambda _s: None,
                clock=lambda: next(clock),
            )

    def test_other_ssm_errors_are_reported(self, clients: _Clients) -> None:
        self._expect_send(clients, "cat /x")
        clients.stubs["ssm"].add_client_error(
            "get_command_invocation",
            service_error_code="AccessDeniedException",
            expected_params={"CommandId": _COMMAND_ID, "InstanceId": _INSTANCE},
        )
        with pytest.raises(
            PcsLoginError, match=r"could not read the command's result.*AccessDenied"
        ):
            pcs_login.read_file(
                NODE, ["cat", "/x"], client_factory=clients.factory, sleep=lambda _s: None
            )
