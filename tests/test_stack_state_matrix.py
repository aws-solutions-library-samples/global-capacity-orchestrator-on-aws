"""State-machine and identity-fence coverage for :mod:`cli.stacks`.

These tests use CloudFormation-shaped mocks only.  They concentrate on the
branches where an unknown/replaced stack, stale change set, cancellation, or
failed dependency phase must stop mutation rather than being treated as a
successful deploy or teardown.
"""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock, call, patch

import pytest
from botocore.exceptions import ClientError

from cli.stacks import StackManager


@pytest.fixture
def manager() -> StackManager:
    config = MagicMock()
    config.project_name = "gco"
    config.global_region = "us-east-2"
    config.api_gateway_region = "us-east-2"
    config.default_region = "us-east-2"
    config.regions = ["us-east-2", "us-west-2"]
    return StackManager(config)


def _error(code: str, message: str, operation: str = "DescribeStacks") -> ClientError:
    return ClientError({"Error": {"Code": code, "Message": message}}, operation)


STACK_NAME = "gco-global"
REGION = "us-east-2"
ACCOUNT = "123456789012"
STACK_ID = f"arn:aws:cloudformation:{REGION}:{ACCOUNT}:stack/{STACK_NAME}/stack-uuid"
CHANGE_NAME = "gco-run-change"
CHANGE_ID = f"arn:aws:cloudformation:{REGION}:{ACCOUNT}:changeSet/{CHANGE_NAME}/change-uuid"


def _stack(status: str = "UPDATE_COMPLETE", **overrides: Any) -> dict[str, Any]:
    value: dict[str, Any] = {
        "StackName": STACK_NAME,
        "StackId": STACK_ID,
        "StackStatus": status,
    }
    value.update(overrides)
    return value


def _change_set(**overrides: Any) -> dict[str, Any]:
    value: dict[str, Any] = {
        "ChangeSetName": CHANGE_NAME,
        "ChangeSetId": CHANGE_ID,
        "StackId": STACK_ID,
        "Status": "CREATE_COMPLETE",
        "ExecutionStatus": "AVAILABLE",
        "StatusReason": "",
        "Tags": [{"Key": "Run", "Value": "test"}],
    }
    value.update(overrides)
    return value


def _prepared(kind: str = "UPDATE", **overrides: str) -> dict[str, dict[str, str]]:
    record = {
        "change_set_id": CHANGE_ID,
        "stack_id": STACK_ID,
        "change_set_type": kind,
    }
    record.update(overrides)
    return {CHANGE_ID: record}


class TestCheckAndFixStuckStack:
    def test_missing_region_fails_closed_only_under_strict_ownership(
        self, manager: StackManager
    ) -> None:
        with patch.object(manager, "_get_deploy_region", return_value=None):
            manager._check_and_fix_stuck_stack(STACK_NAME)
            with pytest.raises(RuntimeError, match="Could not resolve deploy Region"):
                manager._check_and_fix_stuck_stack(STACK_NAME, strict_ownership=True)

    @pytest.mark.parametrize(
        ("exc", "strict", "raises"),
        [
            (_error("ValidationError", "Stack does not exist"), False, False),
            (_error("AccessDenied", "denied"), False, False),
            (_error("AccessDenied", "denied"), True, True),
            (RuntimeError("transport"), False, False),
            (RuntimeError("transport"), True, True),
        ],
    )
    def test_describe_failures_never_delete(
        self, manager: StackManager, exc: Exception, strict: bool, raises: bool
    ) -> None:
        cfn = MagicMock()
        cfn.describe_stacks.side_effect = exc
        contexts: Any = pytest.raises(type(exc)) if raises else _does_not_raise()
        with (
            patch.object(manager, "_get_deploy_region", return_value=REGION),
            patch("boto3.client", return_value=cfn),
            contexts,
        ):
            manager._check_and_fix_stuck_stack(STACK_NAME, strict_ownership=strict)
        cfn.delete_stack.assert_not_called()

    @pytest.mark.parametrize(
        "response",
        [
            {"Stacks": []},
            {"Stacks": [_stack(), _stack()]},
            {"Stacks": [{**_stack(), "StackName": "replacement"}]},
            {"Stacks": [{**_stack(), "StackId": ""}]},
        ],
    )
    def test_ambiguous_identity_never_deletes(
        self, manager: StackManager, response: dict[str, Any]
    ) -> None:
        cfn = MagicMock()
        cfn.describe_stacks.return_value = response
        with (
            patch.object(manager, "_get_deploy_region", return_value=REGION),
            patch("boto3.client", return_value=cfn),
            pytest.raises(RuntimeError, match="invalid identity"),
        ):
            manager._check_and_fix_stuck_stack(STACK_NAME)
        cfn.delete_stack.assert_not_called()

    def test_strict_rejects_uncheckpointed_and_replacement_stack(
        self, manager: StackManager
    ) -> None:
        cfn = MagicMock()
        cfn.describe_stacks.return_value = {"Stacks": [_stack("ROLLBACK_COMPLETE")]}
        with (
            patch.object(manager, "_get_deploy_region", return_value=REGION),
            patch("boto3.client", return_value=cfn),
            pytest.raises(RuntimeError, match="uncheckpointed"),
        ):
            manager._check_and_fix_stuck_stack(STACK_NAME, strict_ownership=True)
        with (
            patch.object(manager, "_get_deploy_region", return_value=REGION),
            patch("boto3.client", return_value=cfn),
            pytest.raises(RuntimeError, match="identity changed"),
        ):
            manager._check_and_fix_stuck_stack(STACK_NAME, expected_stack_id=STACK_ID + "-old")
        cfn.delete_stack.assert_not_called()

    @pytest.mark.parametrize("status", ["UPDATE_COMPLETE", "CREATE_IN_PROGRESS"])
    def test_healthy_or_active_stack_is_left_alone(
        self, manager: StackManager, status: str
    ) -> None:
        cfn = MagicMock()
        cfn.describe_stacks.return_value = {"Stacks": [_stack(status)]}
        with (
            patch.object(manager, "_get_deploy_region", return_value=REGION),
            patch("boto3.client", return_value=cfn),
        ):
            manager._check_and_fix_stuck_stack(STACK_NAME)
        cfn.delete_stack.assert_not_called()

    @pytest.mark.parametrize(
        "status",
        [
            "REVIEW_IN_PROGRESS",
            "ROLLBACK_COMPLETE",
            "ROLLBACK_FAILED",
            "CREATE_FAILED",
            "DELETE_FAILED",
        ],
    )
    def test_stuck_stack_is_authorized_then_deleted_by_exact_arn(
        self, manager: StackManager, status: str
    ) -> None:
        cfn = MagicMock()
        cfn.describe_stacks.return_value = {"Stacks": [_stack(status)]}
        waiter = cfn.get_waiter.return_value
        authorize = MagicMock()
        with (
            patch.object(manager, "_get_deploy_region", return_value=REGION),
            patch("boto3.client", return_value=cfn),
        ):
            manager._check_and_fix_stuck_stack(
                STACK_NAME,
                expected_stack_id=STACK_ID,
                authorize_stack=authorize,
                strict_ownership=True,
            )
        authorize.assert_called_once_with(STACK_NAME, REGION, STACK_ID)
        cfn.delete_stack.assert_called_once_with(StackName=STACK_ID)
        waiter.wait.assert_called_once_with(
            StackName=STACK_ID, WaiterConfig={"Delay": 10, "MaxAttempts": 60}
        )


class TestExactTargetDescription:
    def test_exact_live_identity_wins_without_name_lookup(self, manager: StackManager) -> None:
        cfn = MagicMock()
        exact = _stack("UPDATE_COMPLETE")
        cfn.describe_stacks.return_value = {"Stacks": [exact]}
        with (
            patch.object(manager, "_get_destroy_region", return_value=REGION),
            patch("boto3.client", return_value=cfn),
        ):
            assert manager._describe_stack_target(
                STACK_NAME,
                expected_stack_id=STACK_ID,
                require_expected_identity=True,
            ) == (REGION, cfn, exact)
        cfn.describe_stacks.assert_called_once_with(StackName=STACK_ID)

    def test_exact_tombstone_and_absent_name_is_authoritative_absence(
        self, manager: StackManager
    ) -> None:
        cfn = MagicMock()
        cfn.describe_stacks.side_effect = [
            {"Stacks": [_stack("DELETE_COMPLETE")]},
            _error("ValidationError", "does not exist"),
        ]
        with (
            patch.object(manager, "_get_destroy_region", return_value=REGION),
            patch("boto3.client", return_value=cfn),
        ):
            assert manager._describe_stack_target(STACK_NAME, expected_stack_id=STACK_ID) is None

    def test_absent_exact_rejects_same_name_replacement(self, manager: StackManager) -> None:
        replacement = STACK_ID.replace("stack-uuid", "replacement")
        cfn = MagicMock()
        cfn.describe_stacks.side_effect = [
            _error("ValidationError", "does not exist"),
            {"Stacks": [_stack(StackId=replacement)]},
        ]
        with (
            patch.object(manager, "_get_destroy_region", return_value=REGION),
            patch("boto3.client", return_value=cfn),
            pytest.raises(RuntimeError, match="same-name replacement"),
        ):
            manager._describe_stack_target(STACK_NAME, expected_stack_id=STACK_ID)

    def test_name_only_access_can_be_forbidden(self, manager: StackManager) -> None:
        cfn = MagicMock()
        cfn.describe_stacks.return_value = {"Stacks": [_stack()]}
        with (
            patch.object(manager, "_get_destroy_region", return_value=REGION),
            patch("boto3.client", return_value=cfn),
            pytest.raises(RuntimeError, match="Refusing name-authorized"),
        ):
            manager._describe_stack_target(STACK_NAME, require_expected_identity=True)

    @pytest.mark.parametrize(
        "stacks",
        [[], [_stack(), _stack()], ["not-a-mapping"], [{"StackName": STACK_NAME}]],
    )
    def test_malformed_describe_response_fails_closed(
        self, manager: StackManager, stacks: list[Any]
    ) -> None:
        cfn = MagicMock()
        cfn.describe_stacks.return_value = {"Stacks": stacks}
        with (
            patch.object(manager, "_get_destroy_region", return_value=REGION),
            patch("boto3.client", return_value=cfn),
            pytest.raises(RuntimeError, match="invalid identity"),
        ):
            manager._describe_stack_target(STACK_NAME)


class TestCloudFormationObservation:
    @pytest.mark.parametrize(
        ("stack", "expected"),
        [
            (
                {
                    **_stack(),
                    "LastUpdatedTime": datetime(2026, 1, 2, tzinfo=UTC),
                    "CreationTime": datetime(2026, 1, 1, tzinfo=UTC),
                },
                datetime(2026, 1, 2, tzinfo=UTC),
            ),
            (
                {**_stack(), "CreationTime": datetime(2026, 1, 1, tzinfo=UTC)},
                datetime(2026, 1, 1, tzinfo=UTC),
            ),
            ({**_stack(), "CreationTime": "not-a-datetime"}, None),
        ],
    )
    def test_last_update_time_prefers_update_then_creation(
        self, manager: StackManager, stack: dict[str, Any], expected: datetime | None
    ) -> None:
        cfn = MagicMock()
        cfn.describe_stacks.return_value = {"Stacks": [stack]}
        with (
            patch.object(manager, "_get_destroy_region", return_value=REGION),
            patch("boto3.client", return_value=cfn),
        ):
            assert manager._get_stack_last_update_time(STACK_NAME) == expected

    def test_last_update_lookup_failure_is_unknown(self, manager: StackManager) -> None:
        with patch.object(manager, "_get_destroy_region", side_effect=RuntimeError("bad")):
            assert manager._get_stack_last_update_time(STACK_NAME) is None

    def test_settle_polls_until_terminal(self, manager: StackManager) -> None:
        statuses = MagicMock(side_effect=["UPDATE_IN_PROGRESS", "UPDATE_COMPLETE"])
        with (
            patch.object(manager, "_get_stack_status", statuses),
            patch("cli.stacks.time.monotonic", side_effect=[0.0, 0.1, 0.2]),
            patch("cli.stacks.time.sleep") as sleep,
        ):
            assert (
                manager._wait_for_stack_settle(STACK_NAME, timeout=30, stack_identifier=STACK_ID)
                == "UPDATE_COMPLETE"
            )
        sleep.assert_called_once_with(15.0)
        assert statuses.call_args_list == [call(STACK_NAME, STACK_ID), call(STACK_NAME, STACK_ID)]

    def test_settle_returns_current_status_when_cancelled(self, manager: StackManager) -> None:
        manager._cdk_cancel_event.set()
        with patch.object(manager, "_get_stack_status", return_value="CREATE_IN_PROGRESS"):
            assert manager._wait_for_stack_settle(STACK_NAME, timeout=10) == "CREATE_IN_PROGRESS"

    @pytest.mark.parametrize("events", [[], None])
    def test_latest_event_empty_response_is_none(self, manager: StackManager, events: Any) -> None:
        cfn = MagicMock()
        cfn.describe_stack_events.return_value = {"StackEvents": events}
        with (
            patch.object(manager, "_get_destroy_region", return_value=REGION),
            patch("boto3.client", return_value=cfn),
        ):
            assert manager._get_latest_stack_event(STACK_NAME) is None

    def test_latest_event_failure_is_best_effort(self, manager: StackManager) -> None:
        with patch.object(manager, "_get_destroy_region", side_effect=RuntimeError("offline")):
            assert manager._get_latest_stack_event(STACK_NAME) is None

    def test_delete_heartbeat_renders_fallback_and_truncates_reason(
        self, manager: StackManager, capsys: Any
    ) -> None:
        with patch.object(manager, "_get_latest_stack_event", return_value=None):
            manager._print_stack_delete_heartbeat(STACK_NAME, None)
        assert "status unknown" in capsys.readouterr().out

        event = {
            "Timestamp": datetime(2026, 1, 1, tzinfo=UTC),
            "LogicalResourceId": "Vpc",
            "ResourceStatus": "DELETE_IN_PROGRESS",
            "ResourceStatusReason": "  " + "x " * 300,
        }
        with patch.object(manager, "_get_latest_stack_event", return_value=event):
            manager._print_stack_delete_heartbeat(STACK_NAME, "DELETE_IN_PROGRESS", STACK_ID)
        rendered = capsys.readouterr().out
        assert "2026-01-01T00:00:00+00:00 Vpc DELETE_IN_PROGRESS" in rendered
        assert "..." in rendered


class TestDeleteConvergence:
    def test_invalid_timeout_env_falls_back_and_absence_succeeds(
        self, manager: StackManager, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("GCO_CLOUDFORMATION_DELETE_TIMEOUT_SECONDS", "invalid")
        with patch.object(manager, "_stack_exists_in_cloudformation", return_value=False):
            assert manager._wait_for_stack_delete_convergence(STACK_NAME) is True

    def test_cancellation_fails_without_observing_stack(self, manager: StackManager) -> None:
        manager._cdk_cancel_event.set()
        with patch.object(manager, "_stack_exists_in_cloudformation") as exists:
            assert manager._wait_for_stack_delete_convergence(STACK_NAME, timeout=10) is False
        exists.assert_not_called()

    def test_identity_change_is_not_swallowed(self, manager: StackManager) -> None:
        with (
            patch.object(
                manager,
                "_stack_exists_in_cloudformation",
                side_effect=RuntimeError("replacement"),
            ),
            pytest.raises(RuntimeError, match="replacement"),
        ):
            manager._wait_for_stack_delete_convergence(STACK_NAME, timeout=10)

    @pytest.mark.parametrize(
        ("status", "expected"),
        [
            ("DELETE_COMPLETE", True),
            ("DELETE_FAILED", False),
            ("ROLLBACK_COMPLETE", False),
        ],
    )
    def test_terminal_and_unexpected_states_fail_or_succeed_explicitly(
        self, manager: StackManager, status: str, expected: bool
    ) -> None:
        with (
            patch.object(manager, "_stack_exists_in_cloudformation", return_value=True),
            patch.object(manager, "_print_stack_delete_heartbeat") as heartbeat,
            patch("cli.stacks.time.monotonic", side_effect=[0.0, 0.0, 0.0]),
        ):
            assert (
                manager._wait_for_stack_delete_convergence(
                    STACK_NAME, timeout=10, initial_status=status
                )
                is expected
            )
        if status in {"DELETE_FAILED", "ROLLBACK_COMPLETE"}:
            heartbeat.assert_called()

    def test_transient_presence_failure_still_observes_terminal_status(
        self, manager: StackManager
    ) -> None:
        with (
            patch.object(
                manager,
                "_stack_exists_in_cloudformation",
                side_effect=OSError("transient"),
            ),
            patch("cli.stacks.time.monotonic", side_effect=[0.0, 0.0, 0.0]),
        ):
            assert (
                manager._wait_for_stack_delete_convergence(
                    STACK_NAME, timeout=10, initial_status="DELETE_COMPLETE"
                )
                is True
            )

    def test_poll_prints_heartbeat_and_then_observes_absence(self, manager: StackManager) -> None:
        with (
            patch.object(
                manager,
                "_stack_exists_in_cloudformation",
                side_effect=[True, False],
            ),
            patch.object(manager, "_get_stack_status", return_value="DELETE_IN_PROGRESS"),
            patch.object(manager, "_print_stack_delete_heartbeat") as heartbeat,
            patch("cli.stacks.time.monotonic", side_effect=[0.0, 0.0, 0.0, 0.1]),
            patch("cli.stacks.time.sleep") as sleep,
        ):
            assert (
                manager._wait_for_stack_delete_convergence(
                    STACK_NAME,
                    timeout=10,
                    poll_interval=1,
                    heartbeat_interval=2,
                    expected_stack_id=STACK_ID,
                    require_expected_identity=True,
                )
                is True
            )
        heartbeat.assert_called_once_with(STACK_NAME, "DELETE_IN_PROGRESS", STACK_ID)
        sleep.assert_called_once()

    def test_direct_delete_cancel_absence_active_and_failure_paths(
        self, manager: StackManager
    ) -> None:
        manager._cdk_cancel_event.set()
        with pytest.raises(RuntimeError, match="cancelled"):
            manager._cloudformation_delete_stack(STACK_NAME)
        manager._cdk_cancel_event.clear()

        with patch.object(manager, "_describe_stack_target", return_value=None):
            assert manager._cloudformation_delete_stack(STACK_NAME) is True

        cfn = MagicMock()
        target = (REGION, cfn, _stack("DELETE_IN_PROGRESS"))
        with (
            patch.object(manager, "_describe_stack_target", return_value=target),
            patch.object(manager, "_wait_for_stack_delete_convergence", return_value=True) as wait,
        ):
            assert manager._cloudformation_delete_stack(STACK_NAME) is True
        cfn.delete_stack.assert_not_called()
        wait.assert_called_once_with(
            STACK_NAME,
            initial_status="DELETE_IN_PROGRESS",
            expected_stack_id=STACK_ID,
            require_expected_identity=False,
        )

        cfn.delete_stack.side_effect = RuntimeError("denied")
        target = (REGION, cfn, _stack("UPDATE_COMPLETE"))
        with patch.object(manager, "_describe_stack_target", return_value=target):
            assert manager._cloudformation_delete_stack(STACK_NAME) is False

    @pytest.mark.parametrize(
        ("error", "reason"),
        [
            (
                _error(
                    "ValidationError",
                    "Stack cannot be deleted while TerminationProtection is enabled",
                    "DeleteStack",
                ),
                "ValidationError: Stack cannot be deleted while TerminationProtection is enabled",
            ),
            (RuntimeError("connection reset"), "RuntimeError: connection reset"),
        ],
    )
    def test_refused_direct_delete_tells_the_operator_why(
        self,
        manager: StackManager,
        capsys: pytest.CaptureFixture[str],
        error: BaseException,
        reason: str,
    ) -> None:
        """A teardown barrier that stops on a refused delete must say what CloudFormation said."""
        cfn = MagicMock()
        cfn.delete_stack.side_effect = error
        target = (REGION, cfn, _stack("UPDATE_COMPLETE"))
        with patch.object(manager, "_describe_stack_target", return_value=target):
            assert manager._cloudformation_delete_stack(STACK_NAME) is False

        assert f"CloudFormation did not delete {STACK_NAME}: {reason}" in capsys.readouterr().out


class TestStrictChangeSetPreflight:
    def test_missing_region_and_non_missing_error_fail_closed(self, manager: StackManager) -> None:
        with (
            patch.object(manager, "_get_deploy_region", return_value=None),
            pytest.raises(RuntimeError, match="Could not resolve deploy Region"),
        ):
            manager._preflight_strict_change_set(
                stack_name=STACK_NAME,
                change_set_name=CHANGE_NAME,
                expected_stack_id=None,
                prepared_change_sets={},
            )

        cfn = MagicMock()
        cfn.describe_change_set.side_effect = _error("AccessDenied", "denied", "DescribeChangeSet")
        with (
            patch.object(manager, "_get_deploy_region", return_value=REGION),
            patch("boto3.client", return_value=cfn),
            pytest.raises(RuntimeError, match="Could not preflight"),
        ):
            manager._preflight_strict_change_set(
                stack_name=STACK_NAME,
                change_set_name=CHANGE_NAME,
                expected_stack_id=None,
                prepared_change_sets={},
            )

    @pytest.mark.parametrize(
        "exc",
        [
            _error("ChangeSetNotFound", "missing", "DescribeChangeSet"),
            _error("ValidationError", "stack does not exist", "DescribeChangeSet"),
        ],
    )
    def test_authoritative_missing_change_set_is_clean(
        self, manager: StackManager, exc: ClientError
    ) -> None:
        cfn = MagicMock()
        cfn.describe_change_set.side_effect = exc
        with (
            patch.object(manager, "_get_deploy_region", return_value=REGION),
            patch("boto3.client", return_value=cfn),
        ):
            manager._preflight_strict_change_set(
                stack_name=STACK_NAME,
                change_set_name=CHANGE_NAME,
                expected_stack_id=None,
                prepared_change_sets={},
            )

    @pytest.mark.parametrize(
        ("change", "history", "message"),
        [
            ({"ChangeSetId": ""}, _prepared(), "omitted immutable identities"),
            ({"ChangeSetName": "other"}, _prepared(), "omitted immutable identities"),
            ({}, {}, "lacks checkpoint authority"),
            (
                {},
                _prepared(change_set_id="different"),
                "authority changed",
            ),
            (
                {},
                _prepared(change_set_type="INVALID"),
                "authority changed",
            ),
        ],
    )
    def test_existing_change_set_requires_exact_checkpoint_authority(
        self,
        manager: StackManager,
        change: dict[str, Any],
        history: dict[str, dict[str, str]],
        message: str,
    ) -> None:
        cfn = MagicMock()
        cfn.describe_change_set.return_value = _change_set(**change)
        with (
            patch.object(manager, "_get_deploy_region", return_value=REGION),
            patch("boto3.client", return_value=cfn),
            pytest.raises(RuntimeError, match=message),
        ):
            manager._preflight_strict_change_set(
                stack_name=STACK_NAME,
                change_set_name=CHANGE_NAME,
                expected_stack_id=STACK_ID,
                prepared_change_sets=history,
            )

    def test_valid_checkpointed_change_set_passes(self, manager: StackManager) -> None:
        cfn = MagicMock()
        cfn.describe_change_set.return_value = _change_set()
        with (
            patch.object(manager, "_get_deploy_region", return_value=REGION),
            patch("boto3.client", return_value=cfn),
        ):
            manager._preflight_strict_change_set(
                stack_name=STACK_NAME,
                change_set_name=CHANGE_NAME,
                expected_stack_id=STACK_ID,
                prepared_change_sets=_prepared(),
            )

    @pytest.mark.parametrize(
        ("stack_id", "change_id", "message"),
        [
            ("not-an-arn", CHANGE_ID, "stack identity"),
            (
                STACK_ID.replace("/gco-global/", "/wrong/"),
                CHANGE_ID,
                "expected stack",
            ),
            (STACK_ID, "not-an-arn", "change-set identity"),
            (
                STACK_ID,
                CHANGE_ID.replace(f"/{CHANGE_NAME}/", "/wrong/"),
                "does not name",
            ),
            (
                STACK_ID,
                CHANGE_ID.replace(ACCOUNT, "999999999999"),
                "different AWS authorities",
            ),
        ],
    )
    def test_arn_validator_rejects_unrelated_authorities(
        self, stack_id: str, change_id: str, message: str
    ) -> None:
        with pytest.raises(RuntimeError, match=message):
            StackManager._validate_strict_change_set_arns(
                stack_name=STACK_NAME,
                change_set_name=CHANGE_NAME,
                stack_id=stack_id,
                change_set_id=change_id,
                region=REGION,
            )


class TestStrictChangeSetExecution:
    def _execute(
        self,
        manager: StackManager,
        cfn: MagicMock,
        *,
        expected_stack_id: str | None = STACK_ID,
        history: dict[str, dict[str, str]] | None = None,
        preparation_succeeded: bool = True,
        authorize: Any = None,
        allow_noop: bool = False,
    ) -> bool:
        with (
            patch.object(manager, "_get_deploy_region", return_value=REGION),
            patch("boto3.client", return_value=cfn),
        ):
            return manager._execute_prepared_change_set(
                stack_name=STACK_NAME,
                change_set_name=CHANGE_NAME,
                expected_stack_id=expected_stack_id,
                expected_tags={"Run": "test"},
                prepared_change_sets=history if history is not None else {},
                preparation_succeeded=preparation_succeeded,
                authorize_stack=authorize,
                on_change_set_prepared=MagicMock(),
                allow_noop=allow_noop,
                timeout=42,
            )

    def test_missing_region_and_inspection_error(self, manager: StackManager) -> None:
        with (
            patch.object(manager, "_get_deploy_region", return_value=None),
            pytest.raises(RuntimeError, match="Could not resolve deploy Region"),
        ):
            manager._execute_prepared_change_set(
                stack_name=STACK_NAME,
                change_set_name=CHANGE_NAME,
                expected_stack_id=STACK_ID,
                expected_tags=None,
                prepared_change_sets={},
                preparation_succeeded=True,
                authorize_stack=MagicMock(),
                on_change_set_prepared=MagicMock(),
                allow_noop=False,
                timeout=1,
            )

        cfn = MagicMock()
        cfn.describe_change_set.side_effect = _error("AccessDenied", "denied", "DescribeChangeSet")
        with pytest.raises(RuntimeError, match="Could not inspect"):
            self._execute(manager, cfn)

    def test_fresh_create_retries_stack_style_absence_before_checkpoint(
        self, manager: StackManager
    ) -> None:
        cfn = MagicMock()
        cfn.describe_change_set.side_effect = [
            _error("ValidationError", "stack does not exist", "DescribeChangeSet"),
            _change_set(),
        ]
        review_target = (REGION, cfn, _stack("REVIEW_IN_PROGRESS"))
        with (
            patch("cli.stacks.time.sleep") as sleep,
            patch.object(manager, "_describe_stack_target", return_value=review_target),
            patch.object(manager, "_wait_for_stack_settle", return_value="CREATE_COMPLETE"),
        ):
            assert self._execute(manager, cfn, expected_stack_id=None) is True

        assert cfn.describe_change_set.call_count == 2
        sleep.assert_called_once_with(2.0)
        cfn.execute_change_set.assert_called_once_with(ChangeSetName=CHANGE_ID)

    def test_fresh_create_absence_retry_is_bounded_and_fails_closed(
        self, manager: StackManager
    ) -> None:
        cfn = MagicMock()
        cfn.describe_change_set.side_effect = _error(
            "ValidationError", "stack does not exist", "DescribeChangeSet"
        )
        with (
            patch("cli.stacks.time.sleep") as sleep,
            pytest.raises(RuntimeError, match="did not create"),
        ):
            self._execute(manager, cfn, expected_stack_id=None)

        assert cfn.describe_change_set.call_count == 16
        assert sleep.call_count == 15
        cfn.execute_change_set.assert_not_called()

    @pytest.mark.parametrize(
        ("expected_stack_id", "history"),
        [
            (STACK_ID, {}),
            (None, _prepared("CREATE")),
        ],
    )
    def test_absence_retry_requires_fresh_uncheckpointed_create(
        self,
        manager: StackManager,
        expected_stack_id: str | None,
        history: dict[str, dict[str, str]],
    ) -> None:
        cfn = MagicMock()
        cfn.describe_change_set.side_effect = _error(
            "ValidationError", "stack does not exist", "DescribeChangeSet"
        )
        with (
            patch("cli.stacks.time.sleep") as sleep,
            pytest.raises(RuntimeError, match="Could not inspect"),
        ):
            self._execute(
                manager,
                cfn,
                expected_stack_id=expected_stack_id,
                history=history,
            )

        cfn.describe_change_set.assert_called_once()
        sleep.assert_not_called()
        cfn.execute_change_set.assert_not_called()

    def test_fresh_create_retry_honors_cancellation_before_sleep(
        self, manager: StackManager
    ) -> None:
        cfn = MagicMock()
        cfn.describe_change_set.side_effect = _error(
            "ValidationError", "stack does not exist", "DescribeChangeSet"
        )
        manager._cdk_cancel_event.set()
        with (
            patch("cli.stacks.time.sleep") as sleep,
            pytest.raises(RuntimeError, match="cancelled before ownership checkpoint"),
        ):
            self._execute(manager, cfn, expected_stack_id=None)

        cfn.describe_change_set.assert_called_once()
        sleep.assert_not_called()
        cfn.execute_change_set.assert_not_called()

    @pytest.mark.parametrize(
        ("change", "message"),
        [
            ({"ChangeSetId": ""}, "omitted immutable identities"),
            ({"ChangeSetName": "replacement"}, "identity changed"),
            ({"Tags": []}, "omitted required tag"),
            ({"Status": "CREATE_PENDING"}, "not usable"),
            ({"ExecutionStatus": "OBSOLETE"}, "not usable"),
        ],
    )
    def test_unusable_change_set_never_executes(
        self, manager: StackManager, change: dict[str, Any], message: str
    ) -> None:
        cfn = MagicMock()
        cfn.describe_change_set.return_value = _change_set(**change)
        with pytest.raises(RuntimeError, match=message):
            self._execute(manager, cfn, authorize=MagicMock())
        cfn.execute_change_set.assert_not_called()

    @pytest.mark.parametrize(
        ("history", "expected", "message"),
        [
            (_prepared(change_set_id="other"), STACK_ID, "authority changed"),
            (_prepared(change_set_type="INVALID"), STACK_ID, "invalid type"),
            (_prepared("UPDATE"), None, "unexpectedly performs UPDATE"),
            (_prepared("UPDATE"), STACK_ID + "-old", "targets replacement"),
        ],
    )
    def test_persisted_authority_must_match_target(
        self,
        manager: StackManager,
        history: dict[str, dict[str, str]],
        expected: str | None,
        message: str,
    ) -> None:
        cfn = MagicMock()
        cfn.describe_change_set.return_value = _change_set()
        with pytest.raises(RuntimeError, match=message):
            self._execute(
                manager,
                cfn,
                expected_stack_id=expected,
                history=history,
                authorize=MagicMock(),
            )
        cfn.execute_change_set.assert_not_called()

    def test_available_change_set_requires_current_preparation(self, manager: StackManager) -> None:
        cfn = MagicMock()
        cfn.describe_change_set.return_value = _change_set()
        with pytest.raises(RuntimeError, match="not produced by this preparation"):
            self._execute(
                manager,
                cfn,
                preparation_succeeded=False,
                authorize=MagicMock(),
            )

    def test_update_requires_authorizer_and_unhealthy_settlement_returns_false(
        self, manager: StackManager
    ) -> None:
        cfn = MagicMock()
        cfn.describe_change_set.return_value = _change_set()
        with pytest.raises(RuntimeError, match="lacks exact authorization"):
            self._execute(manager, cfn, authorize=None)
        cfn.execute_change_set.assert_not_called()

        with patch.object(
            manager, "_wait_for_stack_settle", return_value="UPDATE_ROLLBACK_COMPLETE"
        ):
            assert self._execute(manager, cfn, authorize=MagicMock()) is False
        cfn.execute_change_set.assert_called_once_with(ChangeSetName=CHANGE_ID)

    @pytest.mark.parametrize("healthy", [False, True])
    def test_missing_change_set_noop_needs_exact_healthy_authorized_stack(
        self, manager: StackManager, healthy: bool
    ) -> None:
        cfn = MagicMock()
        cfn.describe_change_set.side_effect = _error(
            "ChangeSetNotFound", "missing", "DescribeChangeSet"
        )
        target = (
            REGION,
            cfn,
            _stack("UPDATE_COMPLETE" if healthy else "UPDATE_ROLLBACK_COMPLETE"),
        )
        with patch.object(manager, "_describe_stack_target", return_value=target):
            if healthy:
                authorize = MagicMock()
                assert self._execute(manager, cfn, authorize=authorize, allow_noop=True) is True
                authorize.assert_called_once_with(STACK_NAME, REGION, STACK_ID)
            else:
                with pytest.raises(RuntimeError, match="did not create"):
                    self._execute(manager, cfn, authorize=MagicMock(), allow_noop=True)

    def test_executed_checkpoint_revalidates_exact_healthy_stack(
        self, manager: StackManager
    ) -> None:
        cfn = MagicMock()
        cfn.describe_change_set.return_value = _change_set(ExecutionStatus="EXECUTE_COMPLETE")
        with (
            patch.object(manager, "_describe_stack_target", return_value=None),
            pytest.raises(RuntimeError, match="no healthy exact stack"),
        ):
            self._execute(
                manager,
                cfn,
                history=_prepared(),
                preparation_succeeded=False,
                authorize=MagicMock(),
            )
        cfn.execute_change_set.assert_not_called()

    def test_create_requires_exact_review_stack_before_checkpoint(
        self, manager: StackManager
    ) -> None:
        cfn = MagicMock()
        cfn.describe_change_set.return_value = _change_set()
        with (
            patch.object(manager, "_describe_stack_target", return_value=None),
            pytest.raises(RuntimeError, match="no exact review stack"),
        ):
            self._execute(manager, cfn, expected_stack_id=None)
        cfn.execute_change_set.assert_not_called()


class TestOrchestrationStateMachines:
    STACKS = [
        "gco-global",
        "gco-api-gateway",
        "gco-us-east-2",
        "gco-regional-api-us-east-2",
        "gco-monitoring",
    ]

    @pytest.mark.parametrize("failure_index", [0, 2, 3, 4])
    def test_sequential_deploy_stops_at_failed_dependency_phase(
        self, manager: StackManager, failure_index: int
    ) -> None:
        outcomes = [index != failure_index for index in range(len(self.STACKS))]
        starts = MagicMock()
        completions = MagicMock()
        with (
            patch.object(manager, "list_stacks", return_value=self.STACKS),
            patch.object(manager, "deploy", side_effect=outcomes) as deploy,
        ):
            overall, successful, failed = manager.deploy_orchestrated(
                require_approval=False,
                on_stack_start=starts,
                on_stack_complete=completions,
            )
        assert overall is False
        assert failed == [self.STACKS[failure_index]]
        assert successful == self.STACKS[:failure_index]
        assert deploy.call_count == failure_index + 1
        assert starts.call_count == failure_index + 1
        assert completions.call_count == failure_index + 1

    def test_parallel_regional_failure_blocks_bridges_and_monitoring(
        self, manager: StackManager
    ) -> None:
        stacks = [
            "gco-global",
            "gco-us-east-2",
            "gco-us-west-2",
            "gco-regional-api-us-east-2",
            "gco-monitoring",
        ]
        with (
            patch.object(manager, "list_stacks", return_value=stacks),
            patch.object(manager, "deploy", return_value=True) as deploy,
            patch.object(
                manager,
                "_deploy_stacks_parallel",
                return_value=(["gco-us-east-2"], ["gco-us-west-2"]),
            ) as parallel,
        ):
            overall, successful, failed = manager.deploy_orchestrated(
                require_approval=False, parallel=True
            )
        assert overall is False
        assert successful == ["gco-global", "gco-us-east-2"]
        assert failed == ["gco-us-west-2"]
        parallel.assert_called_once()
        assert deploy.call_count == 1

    @pytest.mark.parametrize(
        ("kwargs", "message"),
        [
            ({"strict_deployment_token": "token"}, "both a run token"),
            (
                {
                    "strict_deployment_token": "token",
                    "on_change_set_prepared": MagicMock(),
                },
                "cannot auto-bootstrap",
            ),
            (
                {
                    "strict_deployment_token": "token",
                    "on_change_set_prepared": MagicMock(),
                    "allow_bootstrap": False,
                },
                "exact authorizer",
            ),
            (
                {
                    "strict_deployment_token": "token",
                    "on_change_set_prepared": MagicMock(),
                    "allow_bootstrap": False,
                    "authorize_stack": MagicMock(),
                },
                "lacks target identities",
            ),
        ],
    )
    def test_strict_deploy_prerequisites_fail_before_mutation(
        self, manager: StackManager, kwargs: dict[str, Any], message: str
    ) -> None:
        with (
            patch.object(manager, "list_stacks", return_value=["gco-global"]),
            patch.object(manager, "deploy") as deploy,
            pytest.raises(RuntimeError, match=message),
        ):
            manager.deploy_orchestrated(**kwargs)
        deploy.assert_not_called()

    def test_destroy_strict_invalid_identity_and_backup_error_stop_before_stacks(
        self, manager: StackManager
    ) -> None:
        with (
            patch.object(manager, "list_stacks", return_value=[STACK_NAME]),
            pytest.raises(RuntimeError, match="invalid stack identities"),
        ):
            manager.destroy_orchestrated(
                expected_stack_ids={STACK_NAME: "not-an-arn"},
                authorize_stack=MagicMock(),
            )

        with (
            patch.object(manager, "list_stacks", return_value=[STACK_NAME]),
            patch.object(manager, "_resolve_strict_teardown_resources", return_value={}),
            patch.object(manager, "_image_registry_destroy_preflight", return_value=True),
            patch.object(manager, "cleanup_orphaned_bastions", return_value=0),
            patch.object(
                manager,
                "_cleanup_backup_vault",
                return_value={"errors": ["vault denied"]},
            ),
            patch.object(manager, "destroy") as destroy,
            pytest.raises(RuntimeError, match="backup-vault cleanup failed"),
        ):
            manager.destroy_orchestrated(
                expected_stack_ids={STACK_NAME: STACK_ID},
                authorize_stack=MagicMock(),
            )
        destroy.assert_not_called()

    def test_phase_barrier_treats_lookup_error_as_present(
        self, manager: StackManager, capsys: Any
    ) -> None:
        with patch.object(
            manager,
            "_stack_exists_in_cloudformation",
            side_effect=[False, OSError("unknown")],
        ):
            remaining = manager._destroy_phase_remaining_stacks("regional", ["gone", "unknown"])
        assert remaining == ["unknown"]
        assert "barrier blocked" in capsys.readouterr().out

    def test_parallel_destroy_interrupt_cancels_workers_before_wait(
        self, manager: StackManager
    ) -> None:
        events: list[str] = []

        class Future:
            def result(self) -> tuple[str, bool]:
                raise KeyboardInterrupt

            def cancel(self) -> bool:
                events.append("future-cancel")
                return True

        future = Future()

        class Executor:
            def submit(self, _fn: Any, _stack_name: str) -> Future:
                return future

            def shutdown(self, *, wait: bool, cancel_futures: bool = False) -> None:
                events.append(f"shutdown:{wait}:{cancel_futures}")

        with (
            patch("cli.stacks.ThreadPoolExecutor", return_value=Executor()),
            patch("cli.stacks.as_completed", return_value=[future]),
            patch.object(
                manager,
                "cancel_active_cdk_processes",
                side_effect=lambda: events.append("cancel-processes"),
            ),
            pytest.raises(KeyboardInterrupt),
        ):
            manager._destroy_stacks_parallel(
                stacks=["gco-us-east-2"],
                force=True,
                on_stack_start=None,
                on_stack_complete=None,
                max_workers=1,
                expected_stack_ids=None,
                authorize_stack=None,
                allow_bootstrap=True,
                bootstrap_stacks=None,
                prepared_change_sets=None,
            )
        assert events == [
            "cancel-processes",
            "future-cancel",
            "shutdown:True:True",
        ]
        assert not manager._cdk_cancel_event.is_set()


def _does_not_raise() -> Any:
    from contextlib import nullcontext

    return nullcontext()


class TestStrictTeardownResourceResolutionMatrix:
    REGIONAL_NAME = "gco-us-east-2"
    REGIONAL_ID = "arn:aws:cloudformation:us-east-2:123456789012:stack/gco-us-east-2/regional-uuid"

    @staticmethod
    def _cfn(resources: list[dict[str, Any]]) -> MagicMock:
        cfn = MagicMock()
        paginator = MagicMock()
        paginator.paginate.return_value = [{"StackResourceSummaries": resources}]
        cfn.get_paginator.return_value = paginator
        return cfn

    @staticmethod
    def _resources(
        *, vpcs: list[str] | None = None, clusters: list[str] | None = None
    ) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for vpc in vpcs or []:
            rows.append({"ResourceType": "AWS::EC2::VPC", "PhysicalResourceId": vpc})
        for cluster in clusters or []:
            rows.append({"ResourceType": "AWS::EKS::Cluster", "PhysicalResourceId": cluster})
        return rows

    def _resolve(
        self,
        manager: StackManager,
        cfn: MagicMock,
        *,
        boto_client: Any,
    ) -> dict[str, dict[str, str]]:
        stack = {
            "StackName": self.REGIONAL_NAME,
            "StackId": self.REGIONAL_ID,
            "StackStatus": "CREATE_COMPLETE",
        }
        with (
            patch.object(
                manager,
                "_describe_stack_target",
                return_value=(REGION, cfn, stack),
            ),
            patch.object(manager, "_get_deploy_region", return_value=REGION),
            patch("boto3.client", side_effect=boto_client),
        ):
            return manager._resolve_strict_teardown_resources(
                stacks=[self.REGIONAL_NAME],
                regional_stacks=[self.REGIONAL_NAME],
                expected_stack_ids={self.REGIONAL_NAME: self.REGIONAL_ID},
                authorize_stack=MagicMock(),
            )

    def test_authoritative_absence_is_skipped(self, manager: StackManager) -> None:
        authorize = MagicMock()
        with patch.object(manager, "_describe_stack_target", return_value=None):
            assert (
                manager._resolve_strict_teardown_resources(
                    stacks=[self.REGIONAL_NAME],
                    regional_stacks=[self.REGIONAL_NAME],
                    expected_stack_ids={self.REGIONAL_NAME: self.REGIONAL_ID},
                    authorize_stack=authorize,
                )
                == {}
            )
        authorize.assert_not_called()

    @pytest.mark.parametrize(
        "resources",
        [
            _resources(vpcs=["vpc-a", "vpc-b"], clusters=["gco-us-east-2"]),
            _resources(vpcs=["vpc-a"], clusters=["cluster-a", "cluster-b"]),
        ],
    )
    def test_ambiguous_exact_stack_resources_fail_closed(
        self, manager: StackManager, resources: list[dict[str, Any]]
    ) -> None:
        cfn = self._cfn(resources)
        with pytest.raises(RuntimeError, match="ambiguous VPC/EKS resources"):
            self._resolve(manager, cfn, boto_client=lambda *_args, **_kwargs: MagicMock())

    @pytest.mark.parametrize(
        ("cluster", "message"),
        [
            (
                {
                    "name": "replacement",
                    "resourcesVpcConfig": {
                        "vpcId": "vpc-exact",
                        "clusterSecurityGroupId": "sg-exact",
                    },
                },
                "changed identity",
            ),
            (
                {
                    "name": "gco-us-east-2",
                    "resourcesVpcConfig": {
                        "vpcId": "vpc-other",
                        "clusterSecurityGroupId": "sg-exact",
                    },
                },
                "no longer belongs",
            ),
            (
                {
                    "name": "gco-us-east-2",
                    "resourcesVpcConfig": {
                        "vpcId": "vpc-exact",
                        "clusterSecurityGroupId": "",
                    },
                },
                "omitted its security-group identity",
            ),
        ],
    )
    def test_live_eks_identity_must_match_exact_stack_resources(
        self, manager: StackManager, cluster: dict[str, Any], message: str
    ) -> None:
        cfn = self._cfn(self._resources(vpcs=["vpc-exact"], clusters=[self.REGIONAL_NAME]))
        eks = MagicMock()
        eks.describe_cluster.return_value = {"cluster": cluster}
        with pytest.raises(RuntimeError, match=message):
            self._resolve(manager, cfn, boto_client=lambda *_args, **_kwargs: eks)

    @pytest.mark.parametrize("groups", [[], [{"GroupId": "sg-exact"}]])
    def test_deleted_cluster_resolves_remaining_sg_inside_exact_vpc(
        self, manager: StackManager, groups: list[dict[str, str]]
    ) -> None:
        cfn = self._cfn(self._resources(vpcs=["vpc-exact"], clusters=[self.REGIONAL_NAME]))
        eks = MagicMock()
        eks.describe_cluster.side_effect = _error(
            "ResourceNotFoundException", "gone", "DescribeCluster"
        )
        ec2 = MagicMock()
        ec2.describe_security_groups.return_value = {"SecurityGroups": groups}

        def client_for(service: str, **_kwargs: Any) -> MagicMock:
            return eks if service == "eks" else ec2

        result = self._resolve(manager, cfn, boto_client=client_for)
        details = result[self.REGIONAL_NAME]
        assert details["vpc_id"] == "vpc-exact"
        assert details["cluster_name"] == self.REGIONAL_NAME
        if groups:
            assert details["cluster_security_group_id"] == "sg-exact"
        else:
            assert "cluster_security_group_id" not in details
        ec2.describe_security_groups.assert_called_once_with(
            Filters=[
                {"Name": "vpc-id", "Values": ["vpc-exact"]},
                {
                    "Name": "tag:aws:eks:cluster-name",
                    "Values": [self.REGIONAL_NAME],
                },
            ]
        )

    def test_deleted_cluster_with_ambiguous_remaining_sgs_fails_closed(
        self, manager: StackManager
    ) -> None:
        cfn = self._cfn(self._resources(vpcs=["vpc-exact"], clusters=[self.REGIONAL_NAME]))
        eks = MagicMock()
        eks.describe_cluster.side_effect = _error(
            "ResourceNotFoundException", "gone", "DescribeCluster"
        )
        ec2 = MagicMock()
        ec2.describe_security_groups.return_value = {
            "SecurityGroups": [{"GroupId": "sg-a"}, {"GroupId": "sg-b"}]
        }

        def client_for(service: str, **_kwargs: Any) -> MagicMock:
            return eks if service == "eks" else ec2

        with pytest.raises(RuntimeError, match="ambiguous EKS security groups"):
            self._resolve(manager, cfn, boto_client=client_for)

    def test_stack_with_only_vpc_or_only_cluster_keeps_known_identifiers(
        self, manager: StackManager
    ) -> None:
        vpc_only = self._cfn(self._resources(vpcs=["vpc-exact"]))
        result = self._resolve(manager, vpc_only, boto_client=lambda *_args, **_kwargs: MagicMock())
        assert result[self.REGIONAL_NAME]["vpc_id"] == "vpc-exact"
        assert "cluster_name" not in result[self.REGIONAL_NAME]

        cluster_only = self._cfn(self._resources(clusters=[self.REGIONAL_NAME]))
        eks = MagicMock()
        eks.describe_cluster.return_value = {
            "cluster": {
                "name": self.REGIONAL_NAME,
                "resourcesVpcConfig": {
                    "vpcId": "vpc-live",
                    "clusterSecurityGroupId": "sg-live",
                },
            }
        }
        result = self._resolve(manager, cluster_only, boto_client=lambda *_args, **_kwargs: eks)
        assert result[self.REGIONAL_NAME]["cluster_security_group_id"] == "sg-live"


class TestBastionCleanupMatrix:
    def test_missing_region_and_inspection_failures_honor_fail_closed(
        self, manager: StackManager, capsys: Any
    ) -> None:
        with patch.object(manager, "_get_deploy_region", return_value=None):
            assert manager._cleanup_orphaned_bastions("gco-unknown") == 0
            with pytest.raises(RuntimeError, match="lacks a Region"):
                manager._cleanup_orphaned_bastions("gco-unknown", fail_closed=True)

        with patch("boto3.client", side_effect=RuntimeError("ec2 denied")):
            assert manager._cleanup_orphaned_bastions("gco-us-east-2", region=REGION) == 0
        assert "could not inspect" in capsys.readouterr().out
        with (
            patch("boto3.client", side_effect=RuntimeError("ec2 denied")),
            pytest.raises(RuntimeError, match="could not inspect"),
        ):
            manager._cleanup_orphaned_bastions("gco-us-east-2", region=REGION, fail_closed=True)

    def test_invalid_vpc_and_lookup_error_are_skipped_or_fatal(self, manager: StackManager) -> None:
        ec2 = MagicMock()
        ec2.describe_vpcs.return_value = {"Vpcs": [{}, {"VpcId": "vpc-good"}]}
        ec2.describe_instances.side_effect = RuntimeError("lookup denied")
        with patch("boto3.client", return_value=ec2):
            assert manager._cleanup_orphaned_bastions("gco-us-east-2", region=REGION) == 0
        with (
            patch("boto3.client", return_value=ec2),
            pytest.raises(RuntimeError, match="has no VPC ID"),
        ):
            manager._cleanup_orphaned_bastions("gco-us-east-2", region=REGION, fail_closed=True)

        ec2.describe_vpcs.return_value = {"Vpcs": [{"VpcId": "vpc-good"}]}
        with (
            patch("boto3.client", return_value=ec2),
            pytest.raises(RuntimeError, match="lookup failed"),
        ):
            manager._cleanup_orphaned_bastions("gco-us-east-2", region=REGION, fail_closed=True)

    def test_selects_only_project_or_legacy_named_bastions_and_deduplicates(
        self, manager: StackManager
    ) -> None:
        from cli.ephemeral_bastion import (
            BASTION_PURPOSE,
            TAG_EPHEMERAL_KEY,
            TAG_PROJECT_KEY,
            TAG_PURPOSE_KEY,
            bastion_instance_name,
        )

        expected_name = bastion_instance_name("gco")

        def instance(
            instance_id: str, project: str | None, *, legacy: bool = False
        ) -> dict[str, Any]:
            tags = [
                {"Key": TAG_EPHEMERAL_KEY, "Value": "true"},
                {"Key": TAG_PURPOSE_KEY, "Value": BASTION_PURPOSE},
            ]
            if project is not None:
                tags.append({"Key": TAG_PROJECT_KEY, "Value": project})
            if legacy:
                tags.append({"Key": "Name", "Value": expected_name})
            return {
                "InstanceId": instance_id,
                "Tags": tags,
                "NetworkInterfaces": [
                    {
                        "NetworkInterfaceId": "eni-primary",
                        "Attachment": {"DeviceIndex": 0, "DeleteOnTermination": True},
                    },
                    {
                        "NetworkInterfaceId": "eni-secondary",
                        "Attachment": {"DeviceIndex": 1, "DeleteOnTermination": True},
                    },
                    {
                        "NetworkInterfaceId": "eni-kept",
                        "Attachment": {"DeviceIndex": 0, "DeleteOnTermination": False},
                    },
                ],
            }

        selected = instance("i-project", "gco")
        duplicate = instance("i-project", "gco")
        legacy = instance("i-legacy", None, legacy=True)
        wrong = instance("i-other", "other", legacy=True)
        ec2 = MagicMock()
        ec2.describe_instances.return_value = {
            "Reservations": [
                {"Instances": [selected, duplicate]},
                {"Instances": [legacy, wrong]},
            ]
        }
        waiter = ec2.get_waiter.return_value
        with (
            patch("boto3.client", return_value=ec2),
            patch.object(
                manager, "_wait_for_bastion_network_interfaces", return_value=set()
            ) as wait_enis,
        ):
            assert (
                manager._cleanup_orphaned_bastions(
                    "gco-us-east-2",
                    region=REGION,
                    vpc_id="vpc-exact",
                    fail_closed=True,
                )
                == 2
            )
        ec2.describe_vpcs.assert_not_called()
        ec2.terminate_instances.assert_called_once_with(InstanceIds=["i-project", "i-legacy"])
        waiter.wait.assert_called_once_with(
            InstanceIds=["i-project", "i-legacy"],
            WaiterConfig={"Delay": 5, "MaxAttempts": 60},
        )
        wait_enis.assert_called_once_with(ec2, ["eni-primary"])

    @pytest.mark.parametrize("strict", [False, True])
    def test_termination_and_wait_failures_follow_strictness(
        self, manager: StackManager, strict: bool
    ) -> None:
        ec2 = MagicMock()
        ec2.describe_instances.return_value = {
            "Reservations": [
                {
                    "Instances": [
                        {
                            "InstanceId": "i-one",
                            "Tags": [{"Key": "gco:project", "Value": "gco"}],
                            "NetworkInterfaces": [],
                        }
                    ]
                }
            ]
        }
        ec2.terminate_instances.side_effect = RuntimeError("terminate denied")
        context = (
            pytest.raises(RuntimeError, match="termination failed") if strict else _does_not_raise()
        )
        with patch("boto3.client", return_value=ec2), context:
            result = manager._cleanup_orphaned_bastions(
                "gco-us-east-2",
                region=REGION,
                vpc_id="vpc-exact",
                fail_closed=strict,
            )
            assert result == 0

        ec2.terminate_instances.side_effect = None
        ec2.get_waiter.return_value.wait.side_effect = RuntimeError("wait timeout")
        context = (
            pytest.raises(RuntimeError, match="did not converge") if strict else _does_not_raise()
        )
        with (
            patch("boto3.client", return_value=ec2),
            patch.object(manager, "_wait_for_bastion_network_interfaces", return_value=set()),
            context,
        ):
            result = manager._cleanup_orphaned_bastions(
                "gco-us-east-2",
                region=REGION,
                vpc_id="vpc-exact",
                fail_closed=strict,
            )
            if not strict:
                assert result == 1

    def test_unreleased_eni_warns_or_fails_closed(self, manager: StackManager, capsys: Any) -> None:
        ec2 = MagicMock()
        ec2.describe_instances.return_value = {
            "Reservations": [
                {
                    "Instances": [
                        {
                            "InstanceId": "i-one",
                            "Tags": [{"Key": "gco:project", "Value": "gco"}],
                            "NetworkInterfaces": [],
                        }
                    ]
                }
            ]
        }
        with (
            patch("boto3.client", return_value=ec2),
            patch.object(
                manager,
                "_wait_for_bastion_network_interfaces",
                return_value={"eni-b", "eni-a"},
            ),
        ):
            assert (
                manager._cleanup_orphaned_bastions(
                    "gco-us-east-2", region=REGION, vpc_id="vpc-exact"
                )
                == 1
            )
        assert "eni-a, eni-b" in capsys.readouterr().out
        with (
            patch("boto3.client", return_value=ec2),
            patch.object(
                manager,
                "_wait_for_bastion_network_interfaces",
                return_value={"eni-a"},
            ),
            pytest.raises(RuntimeError, match="have not released"),
        ):
            manager._cleanup_orphaned_bastions(
                "gco-us-east-2",
                region=REGION,
                vpc_id="vpc-exact",
                fail_closed=True,
            )

    def test_wait_for_enis_handles_absent_empty_available_and_busy(
        self, manager: StackManager
    ) -> None:
        ec2 = MagicMock()
        not_found = _error(
            "InvalidNetworkInterfaceID.NotFound", "gone", "DescribeNetworkInterfaces"
        )

        def describe(*, NetworkInterfaceIds: list[str]) -> dict[str, Any]:
            eni_id = NetworkInterfaceIds[0]
            if eni_id == "eni-missing":
                raise not_found
            if eni_id == "eni-empty":
                return {"NetworkInterfaces": []}
            if eni_id == "eni-delete":
                return {"NetworkInterfaces": [{"Status": "available"}]}
            return {"NetworkInterfaces": [{"Status": "in-use"}]}

        ec2.describe_network_interfaces.side_effect = describe
        sleep = MagicMock()
        fake_time = SimpleNamespace(
            monotonic=MagicMock(side_effect=[0.0, 0.0, 0.0, 1.0]),
            sleep=sleep,
        )
        with patch.dict("sys.modules", {"time": fake_time}):
            remaining = manager._wait_for_bastion_network_interfaces(
                ec2,
                ["eni-missing", "eni-empty", "eni-delete", "eni-busy"],
                timeout_seconds=0.5,
                poll_interval_seconds=0.1,
            )
        assert remaining == {"eni-busy"}
        ec2.delete_network_interface.assert_called_once_with(NetworkInterfaceId="eni-delete")
        assert sleep.call_args_list == [call(0.1), call(0.1)]

    @pytest.mark.parametrize(
        "error",
        [
            _error("AccessDenied", "denied", "DescribeNetworkInterfaces"),
            RuntimeError("offline"),
        ],
    )
    def test_wait_for_eni_inspection_failure_returns_all_remaining(
        self, manager: StackManager, error: Exception
    ) -> None:
        ec2 = MagicMock()
        ec2.describe_network_interfaces.side_effect = error
        assert manager._wait_for_bastion_network_interfaces(ec2, ["eni-a", "eni-b"]) == {
            "eni-a",
            "eni-b",
        }

    @pytest.mark.parametrize(
        ("delete_error", "remaining"),
        [
            (
                _error(
                    "InvalidNetworkInterfaceID.NotFound",
                    "gone",
                    "DeleteNetworkInterface",
                ),
                set(),
            ),
            (
                _error("DependencyViolation", "busy", "DeleteNetworkInterface"),
                {"eni-a"},
            ),
            (RuntimeError("busy"), {"eni-a"}),
        ],
    )
    def test_wait_for_eni_delete_failures_are_idempotent_or_bounded(
        self, manager: StackManager, delete_error: Exception, remaining: set[str]
    ) -> None:
        ec2 = MagicMock()
        ec2.describe_network_interfaces.return_value = {
            "NetworkInterfaces": [{"Status": "available"}]
        }
        ec2.delete_network_interface.side_effect = delete_error
        fake_time = SimpleNamespace(
            monotonic=MagicMock(side_effect=[0.0, 1.0, 1.0]),
            sleep=MagicMock(),
        )
        with patch.dict("sys.modules", {"time": fake_time}):
            assert (
                manager._wait_for_bastion_network_interfaces(ec2, ["eni-a"], timeout_seconds=0.5)
                == remaining
            )


class TestVolumeDeleteBlockerMatrix:
    @pytest.mark.parametrize(
        ("response_or_error", "expected"),
        [
            (
                _error("InvalidVolume.NotFound", "gone", "DescribeVolumes"),
                "already-absent",
            ),
            (
                _error("AccessDenied", "denied", "DescribeVolumes"),
                "recheck-failed: AccessDenied",
            ),
            (RuntimeError("offline"), "recheck-failed: RuntimeError"),
            ({"Volumes": []}, "recheck-returned-ambiguous-identity"),
            (
                {"Volumes": [{"VolumeId": "vol-other"}]},
                "recheck-returned-changed-identity",
            ),
            (
                {"Volumes": [{"VolumeId": "vol-1", "State": "in-use"}]},
                "state-is-in-use",
            ),
            (
                {"Volumes": [{"VolumeId": "vol-1", "State": None}]},
                "state-is-unknown",
            ),
            (
                {
                    "Volumes": [
                        {
                            "VolumeId": "vol-1",
                            "State": "available",
                            "Attachments": [{"InstanceId": "i-1"}],
                        }
                    ]
                },
                "volume-has-attachments",
            ),
            (
                {
                    "Volumes": [
                        {
                            "VolumeId": "vol-1",
                            "State": "available",
                            "Attachments": [],
                            "Tags": [{"Key": "other", "Value": "x"}],
                        }
                    ]
                },
                "cluster-ownership-tag-absent",
            ),
            (
                {
                    "Volumes": [
                        {
                            "VolumeId": "vol-1",
                            "State": "available",
                            "Attachments": [],
                            "Tags": [{"Key": "kubernetes.io/cluster/gco", "Value": "owned"}],
                        }
                    ]
                },
                None,
            ),
        ],
    )
    def test_volume_is_deleted_only_after_exact_readback(
        self, response_or_error: Any, expected: str | None
    ) -> None:
        ec2 = MagicMock()
        if isinstance(response_or_error, Exception):
            ec2.describe_volumes.side_effect = response_or_error
        else:
            ec2.describe_volumes.return_value = response_or_error
        assert (
            StackManager._volume_delete_blocked(
                ec2,
                "vol-1",
                cluster_tag="kubernetes.io/cluster/gco",
            )
            == expected
        )
