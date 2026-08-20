"""Focused policy and Click tests for EBS volume cleanup authorization."""

from dataclasses import FrozenInstanceError
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

from cli.commands.stacks_cmd import stacks
from cli.volume_cleanup import (
    DeletionAuthorizationSource,
    DestroyCommandKind,
    VolumeCleanupRequest,
    VolumePolicy,
    VolumePolicyConflictError,
    resolve_volume_cleanup_request,
)


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


@pytest.fixture
def stack_cli_mocks():
    """Isolate Click tests from stack-manager and AWS-backed behavior."""
    manager = MagicMock()
    manager.destroy.return_value = True
    # These policy tests assert prompt/authorization behavior only, so the shared
    # cleanup helper reports the non-regional contract: no outcome, no EBS work.
    manager.cleanup_regional_volumes_after_destroy.return_value = None
    manager.list_stacks.return_value = ["gco-us-east-1", "gco-global"]
    manager.destroy_orchestrated.return_value = (
        True,
        ["gco-us-east-1", "gco-global"],
        [],
    )
    formatter = MagicMock()

    with (
        patch("cli.stacks.get_stack_manager", return_value=manager) as manager_factory,
        patch(
            "cli.stacks.get_stack_destroy_order",
            return_value=["gco-us-east-1", "gco-global"],
        ) as destroy_order,
        patch(
            "cli.commands.stacks_cmd.get_output_formatter",
            return_value=formatter,
        ) as formatter_factory,
    ):
        yield SimpleNamespace(
            manager=manager,
            manager_factory=manager_factory,
            destroy_order=destroy_order,
            formatter_factory=formatter_factory,
        )


@pytest.mark.parametrize(
    ("command", "retain", "delete", "yes", "policy", "authorized", "source", "prompt"),
    [
        (
            DestroyCommandKind.SINGLE,
            False,
            False,
            False,
            VolumePolicy.RETAIN,
            False,
            DeletionAuthorizationSource.NONE,
            False,
        ),
        (
            DestroyCommandKind.SINGLE,
            False,
            False,
            True,
            VolumePolicy.RETAIN,
            False,
            DeletionAuthorizationSource.NONE,
            False,
        ),
        (
            DestroyCommandKind.SINGLE,
            True,
            False,
            False,
            VolumePolicy.RETAIN,
            False,
            DeletionAuthorizationSource.NONE,
            False,
        ),
        (
            DestroyCommandKind.SINGLE,
            True,
            False,
            True,
            VolumePolicy.RETAIN,
            False,
            DeletionAuthorizationSource.NONE,
            False,
        ),
        (
            DestroyCommandKind.SINGLE,
            False,
            True,
            False,
            VolumePolicy.DELETE,
            False,
            DeletionAuthorizationSource.NONE,
            True,
        ),
        (
            DestroyCommandKind.SINGLE,
            False,
            True,
            True,
            VolumePolicy.DELETE,
            True,
            DeletionAuthorizationSource.EXPLICIT_DELETE_WITH_YES,
            False,
        ),
        (
            DestroyCommandKind.ALL,
            False,
            False,
            False,
            VolumePolicy.RETAIN,
            False,
            DeletionAuthorizationSource.NONE,
            False,
        ),
        (
            DestroyCommandKind.ALL,
            False,
            False,
            True,
            VolumePolicy.DELETE,
            True,
            DeletionAuthorizationSource.DESTROY_ALL_WITH_YES,
            False,
        ),
        (
            DestroyCommandKind.ALL,
            True,
            False,
            False,
            VolumePolicy.RETAIN,
            False,
            DeletionAuthorizationSource.NONE,
            False,
        ),
        (
            DestroyCommandKind.ALL,
            True,
            False,
            True,
            VolumePolicy.RETAIN,
            False,
            DeletionAuthorizationSource.NONE,
            False,
        ),
        (
            DestroyCommandKind.ALL,
            False,
            True,
            False,
            VolumePolicy.DELETE,
            False,
            DeletionAuthorizationSource.NONE,
            True,
        ),
        (
            DestroyCommandKind.ALL,
            False,
            True,
            True,
            VolumePolicy.DELETE,
            True,
            DeletionAuthorizationSource.EXPLICIT_DELETE_WITH_YES,
            False,
        ),
    ],
)
def test_resolves_every_non_conflicting_command_combination(
    command,
    retain,
    delete,
    yes,
    policy,
    authorized,
    source,
    prompt,
):
    decision = resolve_volume_cleanup_request(
        command=command,
        retain_volumes=retain,
        delete_volumes=delete,
        yes=yes,
    )

    assert decision.policy is policy
    assert decision.deletion_authorized is authorized
    assert decision.authorization_source is source
    assert decision.requires_volume_confirmation is prompt


@pytest.mark.parametrize("command", list(DestroyCommandKind))
@pytest.mark.parametrize("yes", [False, True])
def test_rejects_every_conflicting_command_combination(command, yes):
    with pytest.raises(VolumePolicyConflictError, match="cannot be used together"):
        resolve_volume_cleanup_request(
            command=command,
            retain_volumes=True,
            delete_volumes=True,
            yes=yes,
        )


def test_affirmative_volume_confirmation_constructs_authorized_request():
    decision = resolve_volume_cleanup_request(
        command=DestroyCommandKind.SINGLE,
        retain_volumes=False,
        delete_volumes=True,
        yes=False,
    )

    with pytest.raises(ValueError, match="confirmation is still required"):
        _ = decision.request

    assert decision.confirm_volume_deletion() == VolumeCleanupRequest(
        policy=VolumePolicy.DELETE,
        deletion_authorized=True,
        authorization_source=DeletionAuthorizationSource.INTERACTIVE_VOLUME_CONFIRMATION,
    )


@pytest.mark.parametrize(
    ("command_args", "command_kind", "expected_policy", "expected_source"),
    [
        (
            ["destroy", "gco-us-east-1"],
            DestroyCommandKind.SINGLE,
            VolumePolicy.RETAIN,
            DeletionAuthorizationSource.NONE,
        ),
        (
            ["destroy-all"],
            DestroyCommandKind.ALL,
            VolumePolicy.DELETE,
            DeletionAuthorizationSource.DESTROY_ALL_WITH_YES,
        ),
    ],
)
@pytest.mark.parametrize("yes_alias", ["-y", "--yes"])
def test_yes_aliases_have_identical_click_semantics(
    runner,
    stack_cli_mocks,
    command_args,
    command_kind,
    expected_policy,
    expected_source,
    yes_alias,
):
    captured = []

    def capture_decision(**kwargs):
        decision = resolve_volume_cleanup_request(**kwargs)
        captured.append(decision)
        return decision

    with patch(
        "cli.commands.stacks_cmd.resolve_volume_cleanup_request",
        side_effect=capture_decision,
    ):
        result = runner.invoke(stacks, [*command_args, yes_alias])

    assert result.exit_code == 0, result.output
    assert len(captured) == 1
    assert captured[0].policy is expected_policy
    assert captured[0].authorization_source is expected_source
    assert "Are you sure" not in result.output
    assert "Permanently delete" not in result.output
    if command_kind is DestroyCommandKind.SINGLE:
        stack_cli_mocks.manager.destroy.assert_called_once_with(
            stack_name="gco-us-east-1",
            force=True,
        )
    else:
        stack_cli_mocks.manager.destroy_orchestrated.assert_called_once()


@pytest.mark.parametrize(
    ("args", "expected_policy", "expected_source"),
    [
        pytest.param(
            ["destroy-all", "-y", "--retain-volumes"],
            VolumePolicy.RETAIN,
            DeletionAuthorizationSource.NONE,
            id="explicit-retain-overrides-automatic-delete",
        ),
        pytest.param(
            ["destroy-all", "-y", "--delete-volumes"],
            VolumePolicy.DELETE,
            DeletionAuthorizationSource.EXPLICIT_DELETE_WITH_YES,
            id="redundant-explicit-delete-is-accepted",
        ),
    ],
)
def test_destroy_all_automatic_policy_options(
    runner,
    stack_cli_mocks,
    args,
    expected_policy,
    expected_source,
):
    captured = []

    def capture_decision(**kwargs):
        decision = resolve_volume_cleanup_request(**kwargs)
        captured.append(decision)
        return decision

    with patch(
        "cli.commands.stacks_cmd.resolve_volume_cleanup_request",
        side_effect=capture_decision,
    ):
        result = runner.invoke(stacks, args)

    assert result.exit_code == 0, result.output
    assert captured[0].policy is expected_policy
    assert captured[0].authorization_source is expected_source
    assert captured[0].requires_volume_confirmation is False
    assert "Permanently delete" not in result.output
    stack_cli_mocks.manager.destroy_orchestrated.assert_called_once()


@pytest.mark.parametrize(
    ("args", "stack_prompt"),
    [
        (
            ["destroy", "gco-us-east-1", "--delete-volumes"],
            "Are you sure you want to destroy gco-us-east-1?",
        ),
        (
            ["destroy-all", "--delete-volumes"],
            "Are you sure you want to destroy all stacks?",
        ),
    ],
)
def test_interactive_delete_accepts_volume_prompt_before_stack_prompt(
    runner,
    stack_cli_mocks,
    args,
    stack_prompt,
):
    result = runner.invoke(stacks, args, input="y\ny\n")

    assert result.exit_code == 0, result.output
    volume_prompt = "Permanently delete eligible dynamically provisioned EBS volumes?"
    assert result.output.index(volume_prompt) < result.output.index(stack_prompt)
    if args[0] == "destroy":
        stack_cli_mocks.manager.destroy.assert_called_once()
    else:
        stack_cli_mocks.manager.destroy_orchestrated.assert_called_once()


@pytest.mark.parametrize(
    "args",
    [
        ["destroy", "gco-us-east-1", "--delete-volumes"],
        ["destroy-all", "--delete-volumes"],
    ],
)
def test_declining_volume_prompt_aborts_before_manager_creation(
    runner,
    stack_cli_mocks,
    args,
):
    result = runner.invoke(stacks, args, input="n\n")

    assert result.exit_code == 1
    assert "Aborted!" in result.output
    stack_cli_mocks.manager_factory.assert_not_called()
    stack_cli_mocks.formatter_factory.assert_not_called()


@pytest.mark.parametrize(
    "args",
    [
        ["destroy", "gco-us-east-1", "--delete-volumes"],
        ["destroy-all", "--delete-volumes"],
    ],
)
def test_declining_stack_prompt_after_volume_acceptance_performs_no_destroy(
    runner,
    stack_cli_mocks,
    args,
):
    result = runner.invoke(stacks, args, input="y\nn\n")

    assert result.exit_code == 1
    assert "Aborted!" in result.output
    stack_cli_mocks.manager.destroy.assert_not_called()
    stack_cli_mocks.manager.destroy_orchestrated.assert_not_called()


@pytest.mark.parametrize(
    "args",
    [
        ["destroy", "gco-us-east-1", "--retain-volumes", "--delete-volumes"],
        ["destroy-all", "--retain-volumes", "--delete-volumes"],
    ],
)
def test_conflict_is_rejected_before_manager_or_resource_calls(
    runner,
    stack_cli_mocks,
    args,
):
    result = runner.invoke(stacks, args)

    assert result.exit_code == 2
    assert "--retain-volumes and --delete-volumes cannot be used together" in result.output
    stack_cli_mocks.manager_factory.assert_not_called()
    stack_cli_mocks.destroy_order.assert_not_called()
    stack_cli_mocks.formatter_factory.assert_not_called()
    assert stack_cli_mocks.manager.mock_calls == []


def test_policy_models_are_immutable():
    request = VolumeCleanupRequest(
        policy=VolumePolicy.RETAIN,
        deletion_authorized=False,
        authorization_source=DeletionAuthorizationSource.NONE,
    )

    with pytest.raises(FrozenInstanceError):
        request.policy = VolumePolicy.DELETE


def test_rejects_unknown_command_kind():
    with pytest.raises(ValueError, match="unsupported destroy command kind"):
        resolve_volume_cleanup_request(
            command="destroy-everything",
            retain_volumes=False,
            delete_volumes=False,
            yes=False,
        )
