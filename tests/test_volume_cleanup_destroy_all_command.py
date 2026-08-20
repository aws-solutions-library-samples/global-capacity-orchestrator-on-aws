"""Click-level tests for destroy-all volume-cleanup wiring and exit status."""

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

from cli.commands.stacks_cmd import stacks
from cli.volume_cleanup import (
    DeletionAuthorizationSource,
    VolumeCleanupRequest,
    VolumePolicy,
)
from tests.test_stacks_volume_cleanup_barrier import completed_outcome, failed_outcome

_EAST = "gco-us-east-1"
_WEST = "gco-us-west-2"
_GLOBAL = "gco-global"


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


@pytest.fixture
def destroy_all_mocks():
    """Replace the stack manager and formatter so no AWS or CDK work happens."""
    manager = MagicMock()
    manager.list_stacks.return_value = [_EAST, _WEST, _GLOBAL]
    formatter = MagicMock()
    formatter.config = SimpleNamespace(output_format="table")

    with (
        patch("cli.stacks.get_stack_manager", return_value=manager),
        patch(
            "cli.stacks.get_stack_destroy_order",
            return_value=[_EAST, _WEST, _GLOBAL],
        ),
        patch("cli.commands.stacks_cmd.get_output_formatter", return_value=formatter),
        patch("time.sleep"),
    ):
        yield SimpleNamespace(manager=manager, formatter=formatter)


def orchestrator(attempts):
    """Return a destroy_orchestrated stub that replays one script per attempt.

    Each scripted attempt is ``(outcomes, overall, successful, failed)``, where the
    outcomes are published through the cleanup channel exactly as the regional
    volume-cleanup barrier publishes them.
    """
    calls: list[dict] = []

    def destroy_orchestrated(**kwargs):
        outcomes, overall, successful, failed = attempts[len(calls)]
        calls.append(kwargs)
        for outcome in outcomes:
            kwargs["on_cleanup_complete"]("ebs-volumes", outcome.to_dict())
        return overall, list(successful), list(failed)

    return destroy_orchestrated, calls


def messages(formatter):
    printed = []
    for surface in ("print_info", "print_success", "print_warning", "print_error"):
        printed.extend(str(call.args[0]) for call in getattr(formatter, surface).call_args_list)
    return printed


def test_destroy_all_passes_one_resolved_request_and_reports_each_outcome(
    runner,
    destroy_all_mocks,
):
    stub, calls = orchestrator(
        [
            (
                [
                    completed_outcome(_EAST, "us-east-1"),
                    completed_outcome(_WEST, "us-west-2"),
                ],
                True,
                [_EAST, _WEST, _GLOBAL],
                [],
            )
        ]
    )
    destroy_all_mocks.manager.destroy_orchestrated.side_effect = stub

    result = runner.invoke(stacks, ["destroy-all", "-y"])

    assert result.exit_code == 0, result.output
    assert len(calls) == 1
    assert calls[0]["volume_cleanup_request"] == VolumeCleanupRequest(
        policy=VolumePolicy.DELETE,
        deletion_authorized=True,
        authorization_source=DeletionAuthorizationSource.DESTROY_ALL_WITH_YES,
    )
    assert callable(calls[0]["on_cleanup_complete"])
    printed = messages(destroy_all_mocks.formatter)
    assert f"EBS volume cleanup completed for {_EAST}" in printed
    assert f"EBS volume cleanup completed for {_WEST}" in printed
    assert "EBS volume cleanup succeeded for 2 target(s)" in printed


def test_retain_option_sends_the_retain_policy_to_orchestration(runner, destroy_all_mocks):
    stub, calls = orchestrator([([], True, [_EAST, _WEST, _GLOBAL], [])])
    destroy_all_mocks.manager.destroy_orchestrated.side_effect = stub

    result = runner.invoke(stacks, ["destroy-all", "-y", "--retain-volumes"])

    assert result.exit_code == 0, result.output
    assert calls[0]["volume_cleanup_request"] == VolumeCleanupRequest(
        policy=VolumePolicy.RETAIN,
        deletion_authorized=False,
        authorization_source=DeletionAuthorizationSource.NONE,
    )


def test_cleanup_failure_alone_exits_unsuccessfully_without_blaming_stacks(
    runner,
    destroy_all_mocks,
):
    failure = ([failed_outcome(_EAST, "us-east-1")], False, [_EAST, _WEST], [])
    stub, calls = orchestrator([failure, failure, failure])
    destroy_all_mocks.manager.destroy_orchestrated.side_effect = stub

    result = runner.invoke(stacks, ["destroy-all", "-y"])

    assert result.exit_code == 1
    # Retry semantics are unchanged: three orchestrator invocations at most.
    assert len(calls) == 3
    printed = messages(destroy_all_mocks.formatter)
    assert "All stacks were destroyed but EBS volume cleanup was unsuccessful" in printed
    assert not any(message.startswith("Some stacks failed to destroy") for message in printed)
    assert any("cleanup-failed" in message for message in printed)


def test_only_the_last_attempt_outcomes_decide_the_exit_status(runner, destroy_all_mocks):
    stub, calls = orchestrator(
        [
            ([failed_outcome(_EAST, "us-east-1")], False, [_EAST, _WEST], []),
            ([completed_outcome(_EAST, "us-east-1")], True, [_EAST, _WEST, _GLOBAL], []),
        ]
    )
    destroy_all_mocks.manager.destroy_orchestrated.side_effect = stub

    result = runner.invoke(stacks, ["destroy-all", "-y"])

    assert result.exit_code == 0, result.output
    assert len(calls) == 2
    printed = messages(destroy_all_mocks.formatter)
    assert "EBS volume cleanup succeeded for 1 target(s)" in printed


def test_stack_failure_keeps_its_existing_exit_status_and_message(runner, destroy_all_mocks):
    attempt = ([], False, [_WEST], [_EAST])
    stub, calls = orchestrator([attempt, attempt, attempt])
    destroy_all_mocks.manager.destroy_orchestrated.side_effect = stub

    result = runner.invoke(stacks, ["destroy-all", "-y"])

    assert result.exit_code == 1
    assert len(calls) == 3
    printed = messages(destroy_all_mocks.formatter)
    assert f"Some stacks failed to destroy: {_EAST}" in printed
    assert not any("EBS volume cleanup" in message for message in printed)
