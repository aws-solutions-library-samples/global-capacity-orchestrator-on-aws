"""Offline coverage for the live-validation ``ownership/`` layer.

Covers ``scripts/live_release_validation/ownership/{stacks, log_groups, kms,
cleanup_role, ecr, efs_automatic_backups, dynamodb_streams}.py``. The ownership
layer is the durable proof of what a run created and may therefore destroy, so
these tests pin the fail-closed identity matching rather than just the happy
paths: a stack is owned only through persisted prepared-change-set authority
plus the exact ARN and run tag; a log group is owned only when its name derives
from a checkpointed stack resource, its generation (ARN + creation time) was
observed stable, and both authority tags survived tagging; a KMS key is owned
only as an exact ``AWS::KMS::Key`` stack resource whose live tag matches the
run; the delegated log-cleanup helper stack, role, template hash, and STS
principal are re-validated on every touch and any drift refuses to proceed; ECR
residue is retained (never deleted) and accepted only by exact creation or
image identity; EFS automatic-backup recovery points and deleted-table DynamoDB
streams are stripped from inventory only after the service itself proves the
source is gone. Every boto3 client, clock, and filesystem side effect outside
``tmp_path`` is faked; nothing here touches AWS.
"""

from __future__ import annotations

import copy
import hashlib
import json
import re
import uuid
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from botocore.exceptions import ClientError

from scripts.live_release_validation import constants
from scripts.live_release_validation.ownership import cleanup_role as ownership_cleanup_role
from scripts.live_release_validation.ownership import dynamodb_streams as ownership_streams
from scripts.live_release_validation.ownership import ecr as ownership_ecr
from scripts.live_release_validation.ownership import efs_automatic_backups as ownership_efs
from scripts.live_release_validation.ownership import kms as ownership_kms
from scripts.live_release_validation.ownership import log_groups as ownership_log_groups
from scripts.live_release_validation.ownership import stacks as ownership_stacks
from tests._live_validation_patching import patch_live_validation_helper
from tests.test_live_release_validation import _context
from tests.test_live_validation_inventory_cleanup import _paginated_client, _patched_helpers

_REGION = "us-east-1"
_OTHER_REGION = "eu-west-1"
_ACCOUNT = "123456789012"
_PROJECT = "gco-live"
_RUN_ID = "run-123"
_TOKEN = "a" * 32
_STACK = f"{_PROJECT}-{_REGION}"
_STACK_ID = f"arn:aws:cloudformation:{_REGION}:{_ACCOUNT}:stack/{_STACK}/stack-uuid"
_GLOBAL_STACK = f"{_PROJECT}-global"
_GLOBAL_STACK_ID = f"arn:aws:cloudformation:{_REGION}:{_ACCOUNT}:stack/{_GLOBAL_STACK}/global-uuid"
_CHANGE_SET_ID = f"arn:aws:cloudformation:{_REGION}:{_ACCOUNT}:changeSet/{_RUN_ID}/cs-uuid"
_LOG_GROUP = f"{_STACK}-ProviderLogGroup-XYZ"
_LOG_GROUP_ARN = f"arn:aws:logs:{_REGION}:{_ACCOUNT}:log-group:{_LOG_GROUP}"
_CREATED_AT = "2026-08-11T00:00:00+00:00"
_RUN_STARTED_MS = int(datetime.fromisoformat(_CREATED_AT).timestamp() * 1000)
_AUTHORITY_TAGS = {
    constants._RUN_STACK_TAG: _RUN_ID,
    constants._LOG_CLEANUP_TOKEN_TAG: _TOKEN,
}


def _client_error(code: str, operation: str = "Operation", message: str = "boom") -> ClientError:
    return ClientError({"Error": {"Code": code, "Message": message}}, operation)


def _owned_stack(
    name: str = _STACK,
    region: str = _REGION,
    stack_id: str = _STACK_ID,
    **overrides: Any,
) -> dict[str, Any]:
    record = {
        "name": name,
        "region": region,
        "stack_id": stack_id,
        "run_tag": _RUN_ID,
        "authority": "prepared-change-set",
        "change_set_id": _CHANGE_SET_ID,
        "change_set_type": "CREATE",
        "prepared_change_sets": {
            _CHANGE_SET_ID: {
                "change_set_id": _CHANGE_SET_ID,
                "stack_id": stack_id,
                "change_set_type": "CREATE",
            }
        },
    }
    record.update(overrides)
    return record


def _stack_state(**extra: Any) -> dict[str, Any]:
    """Checkpoint state in which this run owns the regional stack by change set."""
    state: dict[str, Any] = {
        "target_stack_regions": {_STACK: _REGION},
        "owned_stacks": {_REGION: {_STACK: _owned_stack()}},
        "log_group_cleanup_token": _TOKEN,
    }
    state.update(extra)
    return state


def _ctx(state: dict[str, Any] | None = None, *, partition: str | None = "aws") -> Any:
    ctx = _context(state=state if state is not None else _stack_state())
    ctx.checkpoint.created_at = _CREATED_AT
    ctx.session.get_partition_for_region.return_value = partition
    return ctx


def _route(ctx: Any, clients: Mapping[str, Any]) -> None:
    """Hand out one fake client per service and refuse anything unexpected."""

    def client(service: str, **kwargs: Any) -> Any:
        if service not in clients:
            raise AssertionError(f"Unexpected service client: {service} {kwargs}")
        return clients[service]

    ctx.session.client.side_effect = client


def _live_stack(
    name: str = _STACK,
    stack_id: str = _STACK_ID,
    status: str = "CREATE_COMPLETE",
    run_tag: str | None = _RUN_ID,
) -> dict[str, Any]:
    tags = {constants._RUN_STACK_TAG: run_tag} if run_tag is not None else {}
    return {"name": name, "stack_id": stack_id, "status": status, "tags": tags}


def _log_group_record(**overrides: Any) -> dict[str, Any]:
    record = {
        "region": _REGION,
        "name": _LOG_GROUP,
        "stack_name": _STACK,
        "stack_id": _STACK_ID,
        "source_resource_type": "AWS::Logs::LogGroup",
        "source_logical_id": "ProviderLogGroup",
        "source_physical_id": _LOG_GROUP,
        "ownership_authority": "cloudformation-stack-resource-derived",
        "authority_phase": "pre-destroy",
        "run_tag": _RUN_ID,
        "cleanup_token": _TOKEN,
    }
    record.update(overrides)
    return record


def _identity(
    creation_time: int = _RUN_STARTED_MS + 1_000,
    tags: Mapping[str, str] | None = None,
    arn: str = _LOG_GROUP_ARN,
) -> dict[str, Any]:
    return {
        "arn": arn,
        "creation_time": creation_time,
        "tags": dict(_AUTHORITY_TAGS if tags is None else tags),
    }


# ---------------------------------------------------------------------------
# ownership/stacks.py
# ---------------------------------------------------------------------------


class TestOwnedStackRecords:
    def test_owned_stacks_creates_and_validates_the_checkpoint_shape(self) -> None:
        ctx = _ctx({})
        assert ownership_stacks._owned_stacks(ctx) == {}
        assert ctx.checkpoint.state["owned_stacks"] == {}

        ctx = _ctx(_stack_state())
        assert ownership_stacks._owned_stack_record(ctx, _REGION, _STACK) == _owned_stack()
        assert ownership_stacks._owned_stack_record(ctx, _OTHER_REGION, _STACK) is None

    @pytest.mark.parametrize(
        ("owned", "match"),
        [
            (["not-a-dict"], "owned_stacks must be an object"),
            ({_REGION: ["not-a-dict"]}, f"ownership for {_REGION} is malformed"),
            ({_REGION: {_STACK: "not-a-dict"}}, f"{_REGION}:{_STACK} is malformed"),
        ],
    )
    def test_malformed_owned_stacks_fail_closed(self, owned: Any, match: str) -> None:
        ctx = _ctx({"owned_stacks": owned})
        with pytest.raises(RuntimeError, match=match):
            ownership_stacks._owned_stacks(ctx)

    @pytest.mark.parametrize(
        "record",
        [
            {"change_set_id": "cs", "change_set_type": "CREATE"},
            {"authority": "prepared-change-set", "change_set_type": "CREATE"},
            {"authority": "prepared-change-set", "change_set_id": "cs", "change_set_type": "X"},
        ],
    )
    def test_prepared_authority_requires_change_set_identity(self, record: dict[str, Any]) -> None:
        with pytest.raises(RuntimeError, match="lacks persisted prepared-change-set authority"):
            ownership_stacks._require_prepared_stack_authority(
                record, region=_REGION, stack_name=_STACK
            )


class TestPreparedChangeSetAuthority:
    def test_target_regions_must_be_an_object(self) -> None:
        with pytest.raises(RuntimeError, match="target_stack_regions must be an object"):
            ownership_stacks._prepared_change_set_authority(_ctx({"target_stack_regions": []}))

    def test_unowned_target_yields_empty_history(self) -> None:
        ctx = _ctx({"target_stack_regions": {_STACK: _REGION}, "owned_stacks": {}})
        assert ownership_stacks._prepared_change_set_authority(ctx) == {_STACK: {}}

    @pytest.mark.parametrize(
        ("prepared", "match"),
        [
            (["cs"], "is malformed"),
            ({1: {"change_set_id": "1"}}, "is malformed"),
            ({_CHANGE_SET_ID: "cs"}, "is malformed"),
            (
                {_CHANGE_SET_ID: {"change_set_id": "other", "stack_id": _STACK_ID}},
                "is inconsistent",
            ),
            (
                {
                    _CHANGE_SET_ID: {
                        "change_set_id": _CHANGE_SET_ID,
                        "stack_id": "other-stack",
                        "change_set_type": "CREATE",
                    }
                },
                "is inconsistent",
            ),
            (
                {
                    _CHANGE_SET_ID: {
                        "change_set_id": _CHANGE_SET_ID,
                        "stack_id": _STACK_ID,
                        "change_set_type": "DELETE",
                    }
                },
                "is inconsistent",
            ),
        ],
    )
    def test_malformed_history_fails_closed(self, prepared: Any, match: str) -> None:
        ctx = _ctx(
            {
                "target_stack_regions": {_STACK: _REGION},
                "owned_stacks": {_REGION: {_STACK: _owned_stack(prepared_change_sets=prepared)}},
            }
        )
        with pytest.raises(RuntimeError, match=match):
            ownership_stacks._prepared_change_set_authority(ctx)


class TestRecordPreparedStackIdentity:
    @pytest.mark.parametrize(
        ("stack_id", "change_set_id", "change_set_type"),
        [("", _CHANGE_SET_ID, "CREATE"), (_STACK_ID, "", "CREATE"), (_STACK_ID, "cs", "DELETE")],
    )
    def test_incomplete_identity_is_refused(
        self, stack_id: str, change_set_id: str, change_set_type: str
    ) -> None:
        ctx = _ctx({})
        with pytest.raises(RuntimeError, match="Invalid prepared change-set identity"):
            ownership_stacks._record_prepared_stack_identity(
                ctx, _STACK, _REGION, stack_id, change_set_id, change_set_type
            )
        assert "owned_stacks" not in ctx.checkpoint.state

    def test_new_record_carries_run_tag_and_history(self) -> None:
        ctx = _ctx({})
        ownership_stacks._record_prepared_stack_identity(
            ctx, _STACK, _REGION, _STACK_ID, _CHANGE_SET_ID, "CREATE"
        )
        record = ctx.checkpoint.state["owned_stacks"][_REGION][_STACK]
        assert record == _owned_stack()
        ctx.persist_callback.assert_called_once_with(ctx.checkpoint)

    def test_prepared_stack_id_change_refuses_adoption(self) -> None:
        ctx = _ctx()
        with pytest.raises(RuntimeError, match="Prepared stack identity changed"):
            ownership_stacks._record_prepared_stack_identity(
                ctx, _STACK, _REGION, f"{_STACK_ID}-replacement", "cs-2", "UPDATE"
            )
        assert ctx.checkpoint.state["owned_stacks"][_REGION][_STACK] == _owned_stack()

    def test_malformed_previous_history_fails_closed(self) -> None:
        ctx = _ctx({"owned_stacks": {_REGION: {_STACK: _owned_stack(prepared_change_sets=["cs"])}}})
        with pytest.raises(RuntimeError, match="is malformed"):
            ownership_stacks._record_prepared_stack_identity(
                ctx, _STACK, _REGION, _STACK_ID, "cs-2", "UPDATE"
            )

    def test_legacy_record_conflicting_with_history_fails_closed(self) -> None:
        conflicting = {
            _CHANGE_SET_ID: {
                "change_set_id": _CHANGE_SET_ID,
                "stack_id": _STACK_ID,
                "change_set_type": "UPDATE",
            }
        }
        ctx = _ctx(
            {"owned_stacks": {_REGION: {_STACK: _owned_stack(prepared_change_sets=conflicting)}}}
        )
        with pytest.raises(RuntimeError, match="is inconsistent"):
            ownership_stacks._record_prepared_stack_identity(
                ctx, _STACK, _REGION, _STACK_ID, "cs-2", "UPDATE"
            )

    def test_reused_change_set_id_with_different_type_is_refused(self) -> None:
        ctx = _ctx()
        with pytest.raises(RuntimeError, match="Prepared change-set identity changed"):
            ownership_stacks._record_prepared_stack_identity(
                ctx, _STACK, _REGION, _STACK_ID, _CHANGE_SET_ID, "UPDATE"
            )


class TestRecordStackIdentity:
    def test_live_stack_must_report_exact_name_and_id(self) -> None:
        with pytest.raises(RuntimeError, match="invalid identity"):
            ownership_stacks._record_stack_identity(
                _ctx(), _STACK, _REGION, _live_stack(name="other")
            )
        with pytest.raises(RuntimeError, match="invalid identity"):
            ownership_stacks._record_stack_identity(
                _ctx(), _STACK, _REGION, _live_stack(stack_id="")
            )

    def test_live_stack_must_carry_this_runs_tag(self) -> None:
        with pytest.raises(RuntimeError, match="is not tagged for run"):
            ownership_stacks._record_stack_identity(
                _ctx(), _STACK, _REGION, _live_stack(run_tag="other-run")
            )

    def test_unprepared_region_or_stack_cannot_be_adopted(self) -> None:
        ctx = _ctx({"owned_stacks": {}})
        with pytest.raises(RuntimeError, match="without prepared-change-set authority"):
            ownership_stacks._record_stack_identity(ctx, _STACK, _REGION, _live_stack())
        ctx = _ctx({"owned_stacks": {_REGION: {}}})
        with pytest.raises(RuntimeError, match="without prepared-change-set authority"):
            ownership_stacks._record_stack_identity(ctx, _STACK, _REGION, _live_stack())

    def test_live_stack_id_differing_from_prepared_id_is_refused(self) -> None:
        ctx = _ctx()
        with pytest.raises(RuntimeError, match="refusing name-based adoption"):
            ownership_stacks._record_stack_identity(
                ctx, _STACK, _REGION, _live_stack(stack_id=f"{_STACK_ID}-replacement")
            )
        assert ctx.checkpoint.state["owned_stacks"][_REGION][_STACK] == _owned_stack()


class TestReconcileStackOwnership:
    def _ctx(self) -> Any:
        return _ctx(_stack_state(enabled_regions=[_REGION]))

    def test_checkpoint_must_name_targets_and_regions(self) -> None:
        with pytest.raises(RuntimeError, match="lacks target stack Regions"):
            ownership_stacks._reconcile_stack_ownership(_ctx({"enabled_regions": [_REGION]}))
        with pytest.raises(RuntimeError, match="lacks target stack Regions"):
            ownership_stacks._reconcile_stack_ownership(
                _ctx({"target_stack_regions": {_STACK: _REGION}})
            )

    def test_project_stacks_outside_the_target_set_fail_closed(self) -> None:
        ctx = self._ctx()
        with (
            patch_live_validation_helper(
                "collect_project_stacks",
                return_value={_REGION: [{"name": _STACK}, {"name": f"{_PROJECT}-orphan"}]},
            ),
            pytest.raises(
                RuntimeError, match=f"outside the checkpoint target set.*{_PROJECT}-orphan"
            ),
        ):
            ownership_stacks._reconcile_stack_ownership(ctx)

    def test_present_stacks_are_verified_and_absent_ones_skipped(self) -> None:
        ctx = self._ctx()
        ctx.checkpoint.state["target_stack_regions"][_GLOBAL_STACK] = _REGION
        ctx.checkpoint.state["owned_stacks"][_REGION][_GLOBAL_STACK] = _owned_stack(
            name=_GLOBAL_STACK, stack_id=_GLOBAL_STACK_ID
        )
        described = {
            _STACK: _live_stack(),
            _GLOBAL_STACK: _live_stack(
                name=_GLOBAL_STACK, stack_id=_GLOBAL_STACK_ID, status="DELETE_COMPLETE"
            ),
        }
        with (
            _patched_helpers(
                {
                    "collect_project_stacks": MagicMock(return_value={_REGION: [{"name": _STACK}]}),
                    "describe_stack": MagicMock(
                        side_effect=lambda session, region, name: described.get(name)
                    ),
                }
            ) as fakes,
        ):
            present = ownership_stacks._reconcile_stack_ownership(ctx)

        assert set(present[_REGION]) == {_STACK}
        assert present[_REGION][_STACK]["stack_id"] == _STACK_ID
        fakes["collect_project_stacks"].assert_called_once_with(ctx.session, [_REGION], _PROJECT)

    @pytest.mark.parametrize(
        ("owned", "match"),
        [
            (
                {_OTHER_REGION: {_STACK: _owned_stack(region=_OTHER_REGION)}},
                "owns unexpected stack identity",
            ),
            ({_REGION: {_STACK: _owned_stack(region=_OTHER_REGION)}}, "Region changed for stack"),
        ],
    )
    def test_checkpoint_records_outside_the_target_set_fail_closed(
        self, owned: dict[str, Any], match: str
    ) -> None:
        ctx = self._ctx()
        ctx.checkpoint.state["owned_stacks"] = owned
        with (
            _patched_helpers(
                {
                    "collect_project_stacks": MagicMock(return_value={}),
                    "describe_stack": MagicMock(return_value=None),
                }
            ),
            pytest.raises(RuntimeError, match=match),
        ):
            ownership_stacks._reconcile_stack_ownership(ctx)


class TestAuthorizeOwnedStack:
    def test_authorization_requires_checkpoint_and_exact_live_identity(self) -> None:
        with pytest.raises(RuntimeError, match="No checkpointed ownership"):
            ownership_stacks._authorize_owned_stack(_ctx({}), _STACK, _REGION, _STACK_ID)
        with pytest.raises(RuntimeError, match="Checkpoint identity changed"):
            ownership_stacks._authorize_owned_stack(_ctx(), _STACK, _REGION, "other-id")

        cases = [
            (None, "disappeared before authorization"),
            (_live_stack(name="other"), "CloudFormation identity changed"),
            (_live_stack(run_tag="other-run"), "Run ownership changed"),
        ]
        for live, match in cases:
            with (
                patch_live_validation_helper("describe_stack", return_value=live),
                pytest.raises(RuntimeError, match=match),
            ):
                ownership_stacks._authorize_owned_stack(_ctx(), _STACK, _REGION, _STACK_ID)

    def test_exact_live_stack_is_authorized(self) -> None:
        ctx = _ctx()
        with patch_live_validation_helper("describe_stack", return_value=_live_stack()) as describe:
            ownership_stacks._authorize_owned_stack(ctx, _STACK, _REGION, _STACK_ID)
        describe.assert_called_once_with(ctx.session, _REGION, _STACK_ID)


class TestTargetStackResolution:
    def _resolve(self, described: Mapping[str, Any], expected_stack_id: str = _STACK_ID) -> Any:
        with patch_live_validation_helper(
            "describe_stack", side_effect=lambda session, region, key: described.get(key)
        ):
            return ownership_stacks._resolve_target_stack(
                _ctx(), region=_REGION, stack_name=_STACK, expected_stack_id=expected_stack_id
            )

    def test_exact_live_stack_resolves_live(self) -> None:
        live = _live_stack()
        assert self._resolve({_STACK_ID: live}) == {"state": "live", "stack": live}

    def test_exact_id_reporting_a_different_name_fails_closed(self) -> None:
        with pytest.raises(RuntimeError, match="Exact stack identity changed"):
            self._resolve({_STACK_ID: _live_stack(name="other")})

    def test_absent_keeps_the_delete_complete_tombstone(self) -> None:
        tombstone = _live_stack(status="DELETE_COMPLETE")
        assert self._resolve({_STACK_ID: tombstone}) == {"state": "absent", "tombstone": tombstone}
        assert self._resolve({}) == {"state": "absent", "tombstone": None}
        assert self._resolve({_STACK: _live_stack(status="DELETE_COMPLETE")}) == {
            "state": "absent",
            "tombstone": None,
        }

    def test_same_name_replacement_and_uncheckpointed_stacks_are_distinguished(self) -> None:
        replacement = _live_stack(stack_id=f"{_STACK_ID}-2")
        assert self._resolve({_STACK: replacement}) == {
            "state": "replacement",
            "stack": replacement,
        }
        assert self._resolve({_STACK: replacement}, expected_stack_id="") == {
            "state": "uncheckpointed",
            "stack": replacement,
        }
        live = _live_stack()
        assert self._resolve({_STACK: live}) == {"state": "live", "stack": live}

    def test_absence_verification_reports_residual_kinds(self) -> None:
        with pytest.raises(RuntimeError, match="lacks target stack Regions"):
            ownership_stacks._verify_target_stack_absence(_ctx({}))

        ctx = _ctx()
        ctx.checkpoint.state["target_stack_regions"][_GLOBAL_STACK] = _REGION
        replacement = _live_stack(name=_GLOBAL_STACK, stack_id=f"{_GLOBAL_STACK_ID}-2")
        with patch_live_validation_helper(
            "describe_stack",
            side_effect=lambda session, region, key: {_GLOBAL_STACK: replacement}.get(key),
        ):
            result = ownership_stacks._verify_target_stack_absence(ctx)

        assert result["all_absent"] is False
        assert result["absent"] == [{"name": _STACK, "region": _REGION, "stack_id": _STACK_ID}]
        assert result["residual"] == [
            {
                "name": _GLOBAL_STACK,
                "region": _REGION,
                "expected_stack_id": None,
                "actual_stack_id": f"{_GLOBAL_STACK_ID}-2",
                "status": "CREATE_COMPLETE",
                "kind": "uncheckpointed",
            }
        ]


# ---------------------------------------------------------------------------
# ownership/dynamodb_streams.py
# ---------------------------------------------------------------------------


class TestExpiredTableStreamStatus:
    def test_unexpected_describe_stream_error_propagates(self) -> None:
        ctx = _ctx()
        streams = MagicMock()
        streams.describe_stream.side_effect = _client_error("AccessDeniedException")
        _route(ctx, {"dynamodbstreams": streams})
        with pytest.raises(ClientError):
            ownership_streams._stream_status(
                ctx, _REGION, f"arn:aws:dynamodb:{_REGION}:{_ACCOUNT}:table/t/stream/1"
            )


# ---------------------------------------------------------------------------
# ownership/efs_automatic_backups.py
# ---------------------------------------------------------------------------


class TestEfsAutomaticBackupPrimitives:
    _POINT_ARN = f"arn:aws:backup:{_REGION}:{_ACCOUNT}:recovery-point:abc-123"

    def test_string_values_accepts_only_strings_and_string_lists(self) -> None:
        assert ownership_efs._string_values("a") == ["a"]
        assert ownership_efs._string_values(["a", "b"]) == ["a", "b"]
        assert ownership_efs._string_values(["a", 1]) is None
        assert ownership_efs._string_values({"a": 1}) is None

    def test_all_principals_requires_the_wildcard(self) -> None:
        assert ownership_efs._all_principals("*") is True
        assert ownership_efs._all_principals({"AWS": "*"}) is True
        assert ownership_efs._all_principals({"AWS": ["*"]}) is True
        assert ownership_efs._all_principals({"Service": "*"}) is False
        assert ownership_efs._all_principals("arn:aws:iam::123456789012:root") is False
        assert ownership_efs._all_principals({"AWS": ["*", "other"]}) is False

    def test_policy_document_must_be_an_object(self) -> None:
        with pytest.raises(RuntimeError, match="must be an object"):
            ownership_efs._policy_has_unconditional_delete_deny("[]", self._POINT_ARN)

    def test_non_deny_and_non_object_statements_are_ignored(self) -> None:
        policy = json.dumps(
            {
                "Statement": [
                    "not-an-object",
                    {"Effect": "Allow", "Principal": "*", "Action": "backup:*", "Resource": "*"},
                ]
            }
        )
        assert ownership_efs._policy_has_unconditional_delete_deny(policy, self._POINT_ARN) is (
            False
        )

    def test_arn_parts_rejects_non_arns(self) -> None:
        assert ownership_efs._arn_parts("not-an-arn") is None
        assert ownership_efs._arn_parts("arn:aws:backup:us-east-1:123") is None
        assert ownership_efs._arn_parts("x:aws:backup:us-east-1:123:recovery-point:a") is None
        assert ownership_efs._arn_parts(self._POINT_ARN) == (
            "aws",
            "backup",
            _REGION,
            _ACCOUNT,
            "recovery-point:abc-123",
        )


class TestAcceptedRecoveryPointBoundaries:
    _VAULT = ownership_efs._EFS_AUTOMATIC_BACKUP_VAULT
    _VAULT_ARN = f"arn:aws:backup:{_REGION}:{_ACCOUNT}:backup-vault:{_VAULT}"
    _POINT_ARN = f"arn:aws:backup:{_REGION}:{_ACCOUNT}:recovery-point:11111111-2222"
    _EFS_ARN = f"arn:aws:elasticfilesystem:{_REGION}:{_ACCOUNT}:file-system/fs-1234567890abcdef0"

    def _clients(self) -> tuple[Any, MagicMock, MagicMock]:
        backup = MagicMock(name="backup")
        backup.describe_recovery_point.return_value = {
            "RecoveryPointArn": self._POINT_ARN,
            "BackupVaultName": self._VAULT,
            "BackupVaultArn": self._VAULT_ARN,
            "ResourceType": "EFS",
            "ResourceName": f"{_PROJECT}-efs",
            "ResourceArn": self._EFS_ARN,
            "Status": "COMPLETED",
            "CalculatedLifecycle": {"DeleteAt": datetime(2030, 1, 1, tzinfo=UTC)},
        }
        backup.describe_backup_vault.return_value = {
            "BackupVaultName": self._VAULT,
            "BackupVaultArn": self._VAULT_ARN,
        }
        backup.get_backup_vault_access_policy.return_value = {
            "Policy": json.dumps(
                {
                    "Statement": [
                        {
                            "Effect": "Deny",
                            "Principal": "*",
                            "Action": "backup:DeleteRecoveryPoint",
                            "Resource": "*",
                        }
                    ]
                }
            )
        }
        backup.list_tags.return_value = {"Tags": {}}
        efs = MagicMock(name="efs")
        efs.describe_file_systems.side_effect = _client_error("FileSystemNotFound")
        ctx = _ctx()
        _route(ctx, {"backup": backup, "efs": efs})
        return ctx, backup, efs

    def test_partition_must_resolve_before_any_backup_call(self) -> None:
        ctx, backup, _efs = self._clients()
        ctx.session.get_partition_for_region.return_value = None
        with pytest.raises(RuntimeError, match="Could not resolve AWS partition"):
            ownership_efs._accepted_recovery_point(
                ctx, region=_REGION, recovery_point_arn=self._POINT_ARN
            )
        backup.describe_recovery_point.assert_not_called()

    def test_unparseable_candidate_arn_is_kept_without_service_calls(self) -> None:
        ctx, backup, _efs = self._clients()
        assert (
            ownership_efs._accepted_recovery_point(
                ctx, region=_REGION, recovery_point_arn="not-an-arn"
            )
            is None
        )
        backup.describe_recovery_point.assert_not_called()

    def test_unparseable_resource_arn_remains_residual(self) -> None:
        ctx, backup, _efs = self._clients()
        backup.describe_recovery_point.return_value["ResourceArn"] = "fs-1234567890abcdef0"
        assert (
            ownership_efs._accepted_recovery_point(
                ctx, region=_REGION, recovery_point_arn=self._POINT_ARN
            )
            is None
        )
        backup.list_tags.assert_not_called()

    def test_vault_identity_mismatch_remains_residual(self) -> None:
        ctx, backup, efs = self._clients()
        backup.describe_backup_vault.return_value["BackupVaultArn"] = (
            f"arn:aws:backup:{_REGION}:{_ACCOUNT}:backup-vault:other"
        )
        assert (
            ownership_efs._accepted_recovery_point(
                ctx, region=_REGION, recovery_point_arn=self._POINT_ARN
            )
            is None
        )
        efs.describe_file_systems.assert_not_called()

    def test_proven_point_is_accepted_with_evidence(self) -> None:
        ctx, _backup, _efs = self._clients()
        evidence = ownership_efs._accepted_recovery_point(
            ctx, region=_REGION, recovery_point_arn=self._POINT_ARN
        )
        assert evidence is not None
        assert evidence["file_system_id"] == "fs-1234567890abcdef0"
        assert evidence["validation_run_tag"] is None
        assert evidence["delete_at"] == "2030-01-01T00:00:00+00:00"


class TestStripEfsAutomaticBackupInventoryShapes:
    def test_inventory_without_regional_mapping_is_returned_unchanged(self) -> None:
        inventory = {"iam_roles": ["gco-live-role"]}
        cleaned, accepted = ownership_efs._strip_accepted_efs_automatic_backup_recovery_points(
            _ctx(), inventory
        )
        assert cleaned == inventory
        assert accepted == []

    @pytest.mark.parametrize(
        ("regional", "match"),
        [
            ({_REGION: ["not-a-dict"]}, "must be an object"),
            (
                {_REGION: {"backup_recovery_points": "arn"}},
                "recovery-point inventory .* must be a list",
            ),
            (
                {_REGION: {"backup_recovery_points": [], "tagged_resources": "x"}},
                "Tagged-resource inventory .* must be a list",
            ),
        ],
    )
    def test_malformed_regional_inventory_fails_closed(self, regional: Any, match: str) -> None:
        with pytest.raises(RuntimeError, match=match):
            ownership_efs._strip_accepted_efs_automatic_backup_recovery_points(
                _ctx(), {"regional": regional}
            )

    def test_region_without_tagged_resources_keeps_other_content(self) -> None:
        inventory = {
            "regional": {
                _REGION: {"backup_recovery_points": [], "dynamodb_tables": [f"{_PROJECT}-jobs"]}
            }
        }
        cleaned, accepted = ownership_efs._strip_accepted_efs_automatic_backup_recovery_points(
            _ctx(), inventory
        )
        assert cleaned == inventory
        assert accepted == []


# ---------------------------------------------------------------------------
# ownership/kms.py
# ---------------------------------------------------------------------------

_KEY_ID = "11111111-2222-3333-4444-555555555555"
_KEY_ARN = f"arn:aws:kms:{_REGION}:{_ACCOUNT}:key/{_KEY_ID}"
_OTHER_KEY_ID = "99999999-8888-7777-6666-555555555555"
_OTHER_KEY_ARN = f"arn:aws:kms:{_REGION}:{_ACCOUNT}:key/{_OTHER_KEY_ID}"
_DELETION_DATE = datetime(2026, 9, 1, tzinfo=UTC)


def _kms_record(**overrides: Any) -> dict[str, Any]:
    record = {
        "region": _REGION,
        "key_id": _KEY_ID,
        "arn": _KEY_ARN,
        "stack_name": _STACK,
        "stack_id": _STACK_ID,
        "logical_id": constants._EKS_KEY_LOGICAL_ID,
        "ownership_authority": "cloudformation-stack-resource",
        "cleanup_policy": "harness-schedule",
        "run_tag": _RUN_ID,
        "scheduled": True,
        "deletion_date": _DELETION_DATE.isoformat(),
    }
    record.update(overrides)
    return record


def _kms_client(
    *,
    metadata: Mapping[str, Any] | Callable[..., Any] | None = None,
    tags: Mapping[str, str] | None = None,
) -> MagicMock:
    kms = MagicMock(name="kms")
    if callable(metadata):
        kms.describe_key.side_effect = metadata
    else:
        kms.describe_key.return_value = {
            "KeyMetadata": dict(
                metadata
                if metadata is not None
                else {"Arn": _KEY_ARN, "KeyState": "Enabled", "Description": "EKS secrets"}
            )
        }
    tag_values = {constants._RUN_STACK_TAG: _RUN_ID} if tags is None else dict(tags)
    kms.list_resource_tags.return_value = {
        "Tags": [{"TagKey": key, "TagValue": value} for key, value in tag_values.items()],
        "Truncated": False,
    }
    return kms


class TestKmsTags:
    def test_tags_follow_the_marker_and_skip_records_without_a_key(self) -> None:
        client = MagicMock()
        client.list_resource_tags.side_effect = [
            {
                "Tags": [{"TagKey": "a", "TagValue": "1"}, {"TagValue": "orphan"}],
                "Truncated": True,
                "NextMarker": "m1",
            },
            {"Tags": [{"TagKey": "b", "TagValue": None}], "Truncated": False, "NextMarker": "x"},
        ]

        assert ownership_kms._kms_tags(client, _KEY_ID) == {"a": "1", "b": ""}
        assert [call.kwargs for call in client.list_resource_tags.call_args_list] == [
            {"KeyId": _KEY_ID},
            {"KeyId": _KEY_ID, "Marker": "m1"},
        ]


class TestValidatedOwnedKmsIdentity:
    def test_exact_retained_eks_key_is_accepted(self) -> None:
        assert ownership_kms._validated_owned_kms_identity(_ctx(), _kms_record()) == (
            _REGION,
            _KEY_ID,
            _KEY_ARN,
            "harness-schedule",
        )

    def test_missing_policy_defaults_to_harness_schedule_only_for_the_retained_key(self) -> None:
        assert (
            ownership_kms._validated_owned_kms_identity(_ctx(), _kms_record(cleanup_policy=None))[3]
            == "harness-schedule"
        )
        with pytest.raises(RuntimeError, match="cleanup policy is invalid"):
            ownership_kms._validated_owned_kms_identity(
                _ctx(), _kms_record(logical_id="OtherKey", cleanup_policy=None)
            )

    def test_non_retained_stack_key_uses_cloudformation_delete(self) -> None:
        record = _kms_record(logical_id="OtherKey", cleanup_policy="cloudformation-delete")
        assert ownership_kms._validated_owned_kms_identity(_ctx(), record)[3] == (
            "cloudformation-delete"
        )
        with pytest.raises(RuntimeError, match="is not harness-retained"):
            ownership_kms._validated_retained_kms_identity(_ctx(), record)
        assert ownership_kms._validated_retained_kms_identity(_ctx(), _kms_record()) == (
            _REGION,
            _KEY_ID,
            _KEY_ARN,
        )

    @pytest.mark.parametrize(
        ("state_override", "record_override", "match"),
        [
            ({"target_stack_regions": None}, {}, "target stack is invalid"),
            ({"target_stack_regions": {_STACK: _OTHER_REGION}}, {}, "target stack is invalid"),
            ({}, {"key_id": ""}, "ARN is invalid"),
            ({}, {"arn": _OTHER_KEY_ARN}, "ARN is invalid"),
            ({}, {"stack_id": f"{_STACK_ID}-2"}, "stack identity is invalid"),
            ({"owned_stacks": {}}, {}, "stack identity is invalid"),
            (
                {"owned_stacks": {_REGION: {_STACK: _owned_stack(run_tag="other-run")}}},
                {},
                "stack identity is invalid",
            ),
            (
                {"owned_stacks": {_REGION: {_STACK: _owned_stack(stack_id="stack/other/id")}}},
                {"stack_id": "stack/other/id"},
                "stack identity is invalid",
            ),
            ({}, {"ownership_authority": "name-match"}, "authority is incomplete"),
            ({}, {"logical_id": ""}, "authority is incomplete"),
            ({}, {"run_tag": "other-run"}, "authority is incomplete"),
            ({}, {"logical_id": "OtherKey"}, "Retained KMS checkpoint identity is invalid"),
            (
                {},
                {"cleanup_policy": "cloudformation-delete"},
                "Retained EKS key cannot use CloudFormation cleanup",
            ),
            ({}, {"cleanup_policy": "manual"}, "cleanup policy is invalid"),
        ],
    )
    def test_identity_drift_fails_closed(
        self, state_override: dict[str, Any], record_override: dict[str, Any], match: str
    ) -> None:
        ctx = _ctx(_stack_state(**state_override))
        with pytest.raises(RuntimeError, match=match):
            ownership_kms._validated_owned_kms_identity(ctx, _kms_record(**record_override))

    def test_empty_stack_name_fails_before_reading_ownership(self) -> None:
        ctx = _ctx(_stack_state(target_stack_regions={"": _REGION}))
        with pytest.raises(RuntimeError, match="stack identity is invalid"):
            ownership_kms._validated_owned_kms_identity(ctx, _kms_record(stack_name=""))

    def test_unresolvable_partition_fails_closed(self) -> None:
        with pytest.raises(RuntimeError, match="Could not resolve AWS partition"):
            ownership_kms._validated_owned_kms_identity(_ctx(partition=None), _kms_record())


class TestCheckpointRetainedKmsKeys:
    """``_checkpoint_retained_kms_keys`` — exact stack-resource authority only."""

    @staticmethod
    def _resource(logical_id: str, key_id: str) -> dict[str, str]:
        return {
            "ResourceType": "AWS::KMS::Key",
            "LogicalResourceId": logical_id,
            "PhysicalResourceId": key_id,
        }

    def _environment(
        self,
        *,
        resources: list[dict[str, Any]] | None = None,
        live_stack: Any = "default",
        kms: MagicMock | None = None,
        state: dict[str, Any] | None = None,
        list_error: ClientError | None = None,
    ) -> Any:
        ctx = _ctx(state if state is not None else _stack_state())
        summaries = (
            resources
            if resources is not None
            else [
                self._resource(constants._EKS_KEY_LOGICAL_ID, _KEY_ID),
                {
                    "ResourceType": "AWS::S3::Bucket",
                    "LogicalResourceId": "B",
                    "PhysicalResourceId": "b",
                },
                {
                    "ResourceType": "AWS::KMS::Key",
                    "LogicalResourceId": "",
                    "PhysicalResourceId": "k",
                },
            ]
        )
        if list_error is not None:

            def paginate(**_kwargs: Any) -> Any:
                raise list_error

            cfn = _paginated_client({"list_stack_resources": paginate})
        else:
            cfn = _paginated_client(
                {"list_stack_resources": [{"StackResourceSummaries": summaries}]}
            )
        kms_client = kms if kms is not None else _kms_client()
        _route(ctx, {"cloudformation": cfn, "kms": kms_client})
        ctx.live_stack = _live_stack() if live_stack == "default" else live_stack
        ctx.cfn = cfn
        ctx.kms = kms_client
        return ctx

    @staticmethod
    def _invoke(ctx: Any, *, describe: Any = None) -> list[dict[str, Any]]:
        describe_stack = (
            describe if describe is not None else MagicMock(return_value=ctx.live_stack)
        )
        with _patched_helpers(
            {
                "_checkpoint_owned_log_groups": MagicMock(return_value=[]),
                "describe_stack": describe_stack,
            }
        ) as fakes:
            records = ownership_kms._checkpoint_retained_kms_keys(ctx)
        fakes["_checkpoint_owned_log_groups"].assert_called_once_with(ctx)
        return records

    def test_live_retained_key_is_checkpointed_with_stack_authority(self) -> None:
        ctx = self._environment()

        records = self._invoke(ctx)

        assert records == [
            {
                "region": _REGION,
                "key_id": _KEY_ID,
                "arn": _KEY_ARN,
                "stack_name": _STACK,
                "stack_id": _STACK_ID,
                "logical_id": constants._EKS_KEY_LOGICAL_ID,
                "ownership_authority": "cloudformation-stack-resource",
                "cleanup_policy": "harness-schedule",
                "run_tag": _RUN_ID,
                "scheduled": False,
                "deletion_date": None,
            }
        ]
        assert ctx.checkpoint.state["owned_kms_keys"] == records
        ctx.cfn.get_paginator("list_stack_resources").paginate.assert_called_once_with(
            StackName=_STACK_ID
        )
        assert ctx.persist_callback.call_count == 2

    def test_non_retained_stack_key_gets_cloudformation_delete_policy(self) -> None:
        state = _stack_state(
            target_stack_regions={_GLOBAL_STACK: _REGION},
            owned_stacks={
                _REGION: {
                    _GLOBAL_STACK: _owned_stack(name=_GLOBAL_STACK, stack_id=_GLOBAL_STACK_ID)
                }
            },
        )
        kms = _kms_client(
            metadata={
                "Arn": _KEY_ARN,
                "KeyState": "PendingDeletion",
                "DeletionDate": _DELETION_DATE,
            }
        )
        ctx = self._environment(
            resources=[self._resource("GlobalKey", _KEY_ID)],
            live_stack=_live_stack(name=_GLOBAL_STACK, stack_id=_GLOBAL_STACK_ID),
            kms=kms,
            state=state,
        )

        records = self._invoke(ctx)

        assert records[0]["cleanup_policy"] == "cloudformation-delete"
        assert records[0]["scheduled"] is True
        assert records[0]["deletion_date"] == _DELETION_DATE.isoformat()

    @pytest.mark.parametrize(
        ("state_override", "match"),
        [
            ({"target_stack_regions": []}, "target_stack_regions must be an object"),
            ({"owned_kms_keys": {}}, "owned_kms_keys must be a list"),
        ],
    )
    def test_malformed_checkpoint_fails_closed(self, state_override: Any, match: str) -> None:
        ctx = self._environment(state=_stack_state(**state_override))
        with pytest.raises(RuntimeError, match=match):
            self._invoke(ctx)

    def test_unowned_target_stack_is_skipped(self) -> None:
        ctx = self._environment(state=_stack_state(owned_stacks={}))
        assert self._invoke(ctx) == []
        ctx.session.client.assert_not_called()

    def test_regional_stack_must_expose_exactly_one_retained_key(self) -> None:
        ctx = self._environment(resources=[self._resource("OtherKey", _KEY_ID)])
        with pytest.raises(RuntimeError, match="Expected one retained EKS KMS key.*found 0"):
            self._invoke(ctx)
        ctx.kms.describe_key.assert_not_called()

    def test_deleted_stack_validation_error_is_tolerated_only_without_a_live_stack(self) -> None:
        ctx = self._environment(list_error=_client_error("ValidationError"), live_stack=None)
        assert self._invoke(ctx) == []

        ctx = self._environment(list_error=_client_error("ValidationError"))
        with pytest.raises(ClientError):
            self._invoke(ctx)

        ctx = self._environment(list_error=_client_error("Throttling"), live_stack=None)
        with pytest.raises(ClientError):
            self._invoke(ctx)

    def test_tombstone_stack_never_creates_authority(self) -> None:
        ctx = self._environment(live_stack=_live_stack(status="DELETE_COMPLETE"))
        assert self._invoke(ctx) == []
        ctx.kms.describe_key.assert_not_called()

    def test_tombstone_stack_marks_persisted_record_deleted_when_key_is_gone(self) -> None:
        previous = _kms_record(scheduled=False, deletion_date=None)
        kms = _kms_client(metadata=MagicMock(side_effect=_client_error("NotFoundException")))
        ctx = self._environment(
            live_stack=None,
            kms=kms,
            state=_stack_state(owned_kms_keys=[previous]),
        )

        records = self._invoke(ctx)

        assert records[0]["scheduled"] is True
        assert records[0]["deleted"] is True

    def test_unexpected_describe_key_error_propagates(self) -> None:
        kms = _kms_client(metadata=MagicMock(side_effect=_client_error("AccessDeniedException")))
        ctx = self._environment(kms=kms)
        with pytest.raises(ClientError):
            self._invoke(ctx)

    def test_missing_key_without_prior_record_is_skipped(self) -> None:
        kms = _kms_client(metadata=MagicMock(side_effect=_client_error("NotFoundException")))
        ctx = self._environment(kms=kms)
        assert self._invoke(ctx) == []

    def test_key_lacking_the_run_tag_fails_closed(self) -> None:
        ctx = self._environment(kms=_kms_client(tags={constants._RUN_STACK_TAG: "other-run"}))
        with pytest.raises(RuntimeError, match="lacks the exact live-validation run tag"):
            self._invoke(ctx)
        assert ctx.checkpoint.state.get("owned_kms_keys", []) == []

    def test_unresolvable_partition_fails_closed(self) -> None:
        ctx = self._environment()
        ctx.session.get_partition_for_region.return_value = None
        with pytest.raises(RuntimeError, match="Could not resolve AWS partition"):
            self._invoke(ctx)

    def test_persisted_record_is_reconciled_not_duplicated(self) -> None:
        previous = _kms_record(scheduled=False, deletion_date=None)
        del previous["cleanup_policy"]
        kms = _kms_client(
            metadata={
                "Arn": _KEY_ARN,
                "KeyState": "PendingDeletion",
                "DeletionDate": _DELETION_DATE,
            }
        )
        ctx = self._environment(kms=kms, state=_stack_state(owned_kms_keys=[previous]))

        records = self._invoke(ctx)

        assert len(records) == 1
        assert records[0]["cleanup_policy"] == "harness-schedule"
        assert records[0]["scheduled"] is True
        assert records[0]["deletion_date"] == _DELETION_DATE.isoformat()

    def test_persisted_record_that_is_not_yet_scheduled_keeps_its_state(self) -> None:
        previous = _kms_record(scheduled=False, deletion_date=None)
        ctx = self._environment(state=_stack_state(owned_kms_keys=[previous]))
        records = self._invoke(ctx)
        assert records == [previous]

    def test_ownership_drift_against_persisted_record_fails_closed(self) -> None:
        previous = _kms_record(logical_id="SomethingElse", cleanup_policy="cloudformation-delete")
        ctx = self._environment(state=_stack_state(owned_kms_keys=[previous]))
        with pytest.raises(RuntimeError, match="KMS ownership changed .*: logical_id"):
            self._invoke(ctx)

    def test_unexpected_live_arn_is_refused_even_past_identity_validation(self) -> None:
        kms = _kms_client(metadata={"Arn": _OTHER_KEY_ARN, "KeyState": "Enabled"})
        ctx = self._environment(kms=kms)
        with (
            patch_live_validation_helper("_validated_owned_kms_identity", return_value=None),
            pytest.raises(RuntimeError, match="unexpected ARN"),
        ):
            self._invoke(ctx)

    def test_stack_losing_authority_before_persist_is_not_checkpointed(self) -> None:
        ctx = self._environment()
        describe = MagicMock(side_effect=[ctx.live_stack, _live_stack(status="DELETE_IN_PROGRESS")])
        assert self._invoke(ctx, describe=describe) == []
        assert describe.call_count == 2


class TestStripExpectedPendingKms:
    def _inventory(
        self, *keys: dict[str, Any], extra: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        resources: dict[str, Any] = {"kms_keys": list(keys)}
        resources.update(extra or {})
        return {"regional": {_REGION: resources}}

    def _ctx(self, *records: dict[str, Any], kms: MagicMock | None = None) -> Any:
        ctx = _ctx(_stack_state(owned_kms_keys=list(records)))
        _route(
            ctx,
            {
                "kms": kms
                if kms is not None
                else _kms_client(
                    metadata={
                        "Arn": _KEY_ARN,
                        "KeyState": "PendingDeletion",
                        "DeletionDate": _DELETION_DATE,
                        "Description": "EKS secrets",
                    }
                )
            },
        )
        return ctx

    def test_pending_key_is_stripped_with_full_evidence(self) -> None:
        ctx = self._ctx(_kms_record())
        inventory = self._inventory({"arn": _KEY_ARN, "key_id": _KEY_ID}, {"arn": _OTHER_KEY_ARN})

        residual, accepted = ownership_kms._strip_expected_pending_kms(ctx, inventory)

        assert residual["regional"][_REGION]["kms_keys"] == [{"arn": _OTHER_KEY_ARN}]
        assert accepted == [
            {
                "region": _REGION,
                "key_id": _KEY_ID,
                "arn": _KEY_ARN,
                "state": "PendingDeletion",
                "description": "EKS secrets",
                "deletion_date": _DELETION_DATE.isoformat(),
                "tags": {constants._RUN_STACK_TAG: _RUN_ID},
                "stack_id": _STACK_ID,
                "logical_id": constants._EKS_KEY_LOGICAL_ID,
                "ownership_authority": "cloudformation-stack-resource",
                "cleanup_policy": "harness-schedule",
                "run_tag": _RUN_ID,
            }
        ]
        assert inventory["regional"][_REGION]["kms_keys"][0]["arn"] == _KEY_ARN

    def test_emptied_region_is_popped_and_absent_key_is_evidence(self) -> None:
        kms = _kms_client(metadata=MagicMock(side_effect=_client_error("NotFoundException")))
        ctx = self._ctx(_kms_record(), kms=kms)

        residual, accepted = ownership_kms._strip_expected_pending_kms(
            ctx, self._inventory({"arn": _KEY_ARN})
        )

        assert residual == {"regional": {}}
        assert accepted[0]["state"] == "Deleted"
        assert accepted[0]["already_absent"] is True
        assert accepted[0]["cleanup_policy"] == "harness-schedule"

    def test_unscheduled_or_duplicate_records_fail_closed(self) -> None:
        with pytest.raises(RuntimeError, match="was not scheduled for deletion"):
            ownership_kms._strip_expected_pending_kms(
                self._ctx(_kms_record(scheduled=False)), self._inventory()
            )
        with pytest.raises(RuntimeError, match="Duplicate KMS checkpoint identity"):
            ownership_kms._strip_expected_pending_kms(
                self._ctx(_kms_record(), _kms_record()), self._inventory()
            )

    def test_unexpected_describe_key_error_propagates(self) -> None:
        kms = _kms_client(metadata=MagicMock(side_effect=_client_error("AccessDeniedException")))
        with pytest.raises(ClientError):
            ownership_kms._strip_expected_pending_kms(
                self._ctx(_kms_record(), kms=kms), self._inventory()
            )

    @pytest.mark.parametrize(
        ("metadata", "tags", "match"),
        [
            (
                {
                    "Arn": _OTHER_KEY_ARN,
                    "KeyState": "PendingDeletion",
                    "DeletionDate": _DELETION_DATE,
                },
                None,
                "ARN changed",
            ),
            (
                {"Arn": _KEY_ARN, "KeyState": "Enabled"},
                None,
                "to be PendingDeletion; found Enabled",
            ),
            (
                {"Arn": _KEY_ARN, "KeyState": "PendingDeletion", "DeletionDate": _DELETION_DATE},
                {constants._RUN_STACK_TAG: "other-run"},
                "run ownership changed",
            ),
            ({"Arn": _KEY_ARN, "KeyState": "PendingDeletion"}, None, "deletion date changed"),
            (
                {
                    "Arn": _KEY_ARN,
                    "KeyState": "PendingDeletion",
                    "DeletionDate": datetime(2027, 1, 1, tzinfo=UTC),
                },
                None,
                "deletion date changed",
            ),
        ],
    )
    def test_live_key_drift_fails_closed(
        self, metadata: dict[str, Any], tags: dict[str, str] | None, match: str
    ) -> None:
        ctx = self._ctx(_kms_record(), kms=_kms_client(metadata=metadata, tags=tags))
        with pytest.raises(RuntimeError, match=match):
            ownership_kms._strip_expected_pending_kms(ctx, self._inventory())


# ---------------------------------------------------------------------------
# ownership/cleanup_role.py
# ---------------------------------------------------------------------------

_USER_ARN = f"arn:aws:iam::{_ACCOUNT}:user/validator"
_USER_ID = "AIDAEXAMPLEUSERID"
_ROLE_NAME = "ValidatorRole"
_ROLE_ARN = f"arn:aws:iam::{_ACCOUNT}:role/{_ROLE_NAME}"
_ROLE_ID = "AROAEXAMPLEROLEID"
_ASSUMED_ARN = f"arn:aws:sts::{_ACCOUNT}:assumed-role/{_ROLE_NAME}/session-1"


def _helper_state(**extra: Any) -> dict[str, Any]:
    state = _stack_state(owned_log_groups=[_log_group_record()], account_arn=_USER_ARN)
    state.update(extra)
    return state


def _iam_client(
    *, user: Mapping[str, Any] | None = "default", role: Mapping[str, Any] | None = None
) -> MagicMock:
    iam = MagicMock(name="iam")
    if user == "default":
        user = {"Arn": _USER_ARN, "UserId": _USER_ID}
    iam.get_user.return_value = {"User": dict(user)} if user is not None else {}
    iam.get_role.return_value = {"Role": dict(role)} if role is not None else {}
    return iam


class TestCleanupPrincipalIdentity:
    def _resolve(self, caller_arn: str, iam: MagicMock | None = None, **ctx_kwargs: Any) -> Any:
        ctx = _ctx(**ctx_kwargs)
        _route(ctx, {"iam": iam if iam is not None else _iam_client()})
        return ownership_cleanup_role._cleanup_principal_identity(ctx, caller_arn)

    def test_partition_and_caller_arn_must_be_exact(self) -> None:
        with pytest.raises(RuntimeError, match="Could not resolve AWS partition"):
            self._resolve(_USER_ARN, partition=None)
        with pytest.raises(RuntimeError, match="empty or contains a wildcard"):
            self._resolve("")
        with pytest.raises(RuntimeError, match="empty or contains a wildcard"):
            self._resolve(f"arn:aws:iam::{_ACCOUNT}:user/*")

    def test_iam_user_resolves_to_its_immutable_id(self) -> None:
        assert self._resolve(_USER_ARN) == {"arn": _USER_ARN, "principal_id": _USER_ID}
        with pytest.raises(RuntimeError, match="invalid user identity"):
            self._resolve(_USER_ARN, _iam_client(user={"Arn": _USER_ARN}))
        with pytest.raises(RuntimeError, match="invalid user identity"):
            self._resolve(_USER_ARN, _iam_client(user={"Arn": f"{_USER_ARN}2", "UserId": _USER_ID}))
        with pytest.raises(RuntimeError, match="invalid user identity"):
            self._resolve(_USER_ARN, _iam_client(user=None))

    def test_iam_role_resolves_to_its_immutable_id(self) -> None:
        role = {"Arn": _ROLE_ARN, "RoleId": _ROLE_ID}
        assert self._resolve(_ROLE_ARN, _iam_client(role=role)) == {
            "arn": _ROLE_ARN,
            "principal_id": _ROLE_ID,
        }
        with pytest.raises(RuntimeError, match="invalid role identity"):
            self._resolve(_ROLE_ARN, _iam_client(role={"Arn": _ROLE_ARN}))

    def test_assumed_role_session_resolves_to_the_underlying_role(self) -> None:
        role = {"Arn": _ROLE_ARN, "RoleId": _ROLE_ID}
        assert self._resolve(_ASSUMED_ARN, _iam_client(role=role)) == {
            "arn": _ROLE_ARN,
            "principal_id": _ROLE_ID,
        }
        pathed = f"arn:aws:sts::{_ACCOUNT}:assumed-role/team/{_ROLE_NAME}/session-1"
        assert self._resolve(pathed, _iam_client(role=role))["arn"] == _ROLE_ARN

    @pytest.mark.parametrize(
        "caller_arn",
        [
            f"arn:aws:sts::{_ACCOUNT}:assumed-role/{_ROLE_NAME}",
            f"arn:aws:sts::{_ACCOUNT}:assumed-role//session-1",
            f"arn:aws:sts::{_ACCOUNT}:assumed-role/{_ROLE_NAME}/",
        ],
    )
    def test_malformed_assumed_role_arns_fail_closed(self, caller_arn: str) -> None:
        iam = _iam_client(role={"Arn": _ROLE_ARN, "RoleId": _ROLE_ID})
        with pytest.raises(RuntimeError, match="Malformed assumed-role caller ARN"):
            self._resolve(caller_arn, iam)
        iam.get_role.assert_not_called()

    @pytest.mark.parametrize(
        "role",
        [
            {"Arn": f"arn:aws:iam::{_ACCOUNT}:role/Other", "RoleId": _ROLE_ID},
            {"Arn": f"arn:aws:iam::999999999999:role/{_ROLE_NAME}", "RoleId": _ROLE_ID},
            {"Arn": _ROLE_ARN},
        ],
    )
    def test_assumed_role_with_drifted_underlying_role_fails_closed(
        self, role: dict[str, Any]
    ) -> None:
        with pytest.raises(RuntimeError, match="invalid underlying role identity"):
            self._resolve(_ASSUMED_ARN, _iam_client(role=role))

    def test_other_principal_kinds_are_refused(self) -> None:
        with pytest.raises(RuntimeError, match="requires an exact IAM user or STS assumed-role"):
            self._resolve(f"arn:aws:iam::{_ACCOUNT}:root")
        with pytest.raises(RuntimeError, match="requires an exact IAM user or STS assumed-role"):
            self._resolve(f"arn:aws:sts::{_ACCOUNT}:federated-user/bob")


class TestLogCleanupPolicy:
    def test_policy_is_conditioned_on_both_authority_tags(self) -> None:
        ctx = _ctx()
        policy, partition = ownership_cleanup_role._log_cleanup_policy(
            ctx, _TOKEN, [_log_group_record()]
        )
        assert partition == "aws"
        assert policy == {
            "Version": "2012-10-17",
            "Statement": [
                {
                    "Effect": "Allow",
                    "Action": "logs:DeleteLogGroup",
                    "Resource": f"arn:aws:logs:*:{_ACCOUNT}:log-group:*",
                    "Condition": {
                        "StringEquals": {
                            f"aws:ResourceTag/{constants._RUN_STACK_TAG}": _RUN_ID,
                            f"aws:ResourceTag/{constants._LOG_CLEANUP_TOKEN_TAG}": _TOKEN,
                        }
                    },
                }
            ],
        }

    def test_partition_must_resolve_for_every_group(self) -> None:
        ctx = _ctx()
        ctx.session.get_partition_for_region.side_effect = ["aws", None]
        with pytest.raises(RuntimeError, match="Could not resolve AWS partition"):
            ownership_cleanup_role._log_cleanup_policy(ctx, _TOKEN, [_log_group_record()])

    def test_groups_spanning_partitions_are_refused(self) -> None:
        other_stack = f"{_PROJECT}-{_OTHER_REGION}"
        other_stack_id = (
            f"arn:aws-cn:cloudformation:{_OTHER_REGION}:{_ACCOUNT}:stack/{other_stack}/other-uuid"
        )
        state = _stack_state()
        state["target_stack_regions"][other_stack] = _OTHER_REGION
        state["owned_stacks"][_OTHER_REGION] = {
            other_stack: _owned_stack(other_stack, _OTHER_REGION, other_stack_id)
        }
        ctx = _ctx(state)
        ctx.session.get_partition_for_region.side_effect = {
            _REGION: "aws",
            _OTHER_REGION: "aws-cn",
        }.__getitem__
        records = [
            _log_group_record(),
            _log_group_record(
                region=_OTHER_REGION,
                stack_name=other_stack,
                stack_id=other_stack_id,
                name="other-group",
                source_physical_id="other-group",
            ),
        ]
        with pytest.raises(RuntimeError, match="share one AWS partition"):
            ownership_cleanup_role._log_cleanup_policy(ctx, _TOKEN, records)


class TestLogCleanupHelperSpec:
    @staticmethod
    def _spec(state: dict[str, Any], iam: MagicMock | None = None, **ctx_kwargs: Any) -> Any:
        ctx = _ctx(state, **ctx_kwargs)
        _route(ctx, {"iam": iam if iam is not None else _iam_client()})
        return ownership_cleanup_role._log_cleanup_helper_spec(ctx)

    def test_spec_is_deterministic_and_scoped_to_the_first_caller(self) -> None:
        spec = self._spec(_helper_state())

        stable_id = uuid.uuid5(
            constants._LOG_CLEANUP_HELPER_NAMESPACE, f"aws:{_ACCOUNT}:{_RUN_ID}:{_TOKEN}"
        ).hex[:20]
        stack_name = f"{constants._LOG_CLEANUP_HELPER_STACK_PREFIX}-{stable_id}"
        assert spec["stack_name"] == stack_name
        assert spec["role_name"] == stack_name
        assert spec["role_arn"] == f"arn:aws:iam::{_ACCOUNT}:role/{stack_name}"
        assert spec["region"] == _REGION
        assert spec["partition"] == "aws"
        assert spec["cleanup_token"] == _TOKEN
        assert spec["first_caller_arn"] == _USER_ARN
        assert spec["trusted_principal_arn"] == _USER_ARN
        assert spec["trusted_principal_id"] == _USER_ID
        assert spec["trust_policy"]["Statement"][0]["Principal"] == {"AWS": _USER_ARN}
        assert spec["trust_policy"]["Statement"][0]["Condition"] == {
            "StringEquals": {"sts:ExternalId": _TOKEN}
        }
        template = spec["template"]
        role_properties = template["Resources"]["CleanupRole"]["Properties"]
        assert role_properties["RoleName"] == stack_name
        assert role_properties["Policies"] == [
            {
                "PolicyName": constants._LOG_CLEANUP_ROLE_POLICY_NAME,
                "PolicyDocument": spec["role_policy"],
            }
        ]
        assert spec["template_body"] == json.dumps(template, separators=(",", ":"), sort_keys=True)
        assert (
            spec["template_sha256"]
            == hashlib.sha256(spec["template_body"].encode("utf-8")).hexdigest()
        )
        assert json.loads(spec["template_body"]) == template

    def test_spec_reuses_the_checkpointed_immutable_caller_identity(self) -> None:
        iam = _iam_client()
        spec = self._spec(
            _helper_state(
                log_cleanup_helper={
                    "first_caller_arn": _ASSUMED_ARN,
                    "trusted_principal_arn": _ROLE_ARN,
                    "trusted_principal_id": _ROLE_ID,
                }
            ),
            iam,
        )
        assert spec["first_caller_arn"] == _ASSUMED_ARN
        assert spec["trusted_principal_arn"] == _ROLE_ARN
        assert spec["trusted_principal_id"] == _ROLE_ID
        iam.get_user.assert_not_called()
        iam.get_role.assert_not_called()

    def test_no_owned_groups_means_no_helper(self) -> None:
        assert self._spec(_stack_state(owned_log_groups=[])) is None

    @pytest.mark.parametrize(
        ("state_override", "match"),
        [
            ({"owned_log_groups": {}}, "owned_log_groups must be a list"),
            ({"log_group_cleanup_token": "short"}, "cleanup token is malformed"),
            ({"owned_log_groups": ["not-a-dict"]}, "must contain objects"),
            ({"log_cleanup_helper": "not-a-dict"}, "log_cleanup_helper must be an object"),
            (
                {"log_cleanup_helper": {"first_caller_arn": _USER_ARN}},
                "lacks immutable caller identity",
            ),
            (
                {
                    "log_cleanup_helper": {
                        "first_caller_arn": _USER_ARN,
                        "trusted_principal_arn": f"arn:aws:iam::{_ACCOUNT}:root",
                        "trusted_principal_id": _USER_ID,
                    }
                },
                "canonical principal is invalid",
            ),
            (
                {
                    "log_cleanup_helper": {
                        "first_caller_arn": _USER_ARN,
                        "trusted_principal_arn": f"arn:aws:iam::{_ACCOUNT}:user/*",
                        "trusted_principal_id": _USER_ID,
                    }
                },
                "canonical principal is invalid",
            ),
            (
                {
                    "log_cleanup_helper": {
                        "first_caller_arn": _USER_ARN,
                        "trusted_principal_arn": _USER_ARN,
                        "trusted_principal_id": "lowercase-id",
                    }
                },
                "canonical principal is invalid",
            ),
        ],
    )
    def test_malformed_checkpoint_or_principal_fails_closed(
        self, state_override: dict[str, Any], match: str
    ) -> None:
        with pytest.raises(RuntimeError, match=match):
            self._spec(_helper_state(**state_override))

    def test_helper_region_must_share_the_groups_partition(self) -> None:
        ctx = _ctx(_helper_state())
        ctx.config.global_region = "us-gov-west-1"
        ctx.session.get_partition_for_region.side_effect = {
            _REGION: "aws",
            "us-gov-west-1": "aws-us-gov",
        }.__getitem__
        with pytest.raises(RuntimeError, match="outside the log groups' AWS partition"):
            ownership_cleanup_role._log_cleanup_helper_spec(ctx)

    def test_helper_names_may_never_collide_with_project_inventory(self) -> None:
        ctx = _ctx(_helper_state())
        ctx.config.project_name = constants._LOG_CLEANUP_HELPER_STACK_PREFIX
        _route(ctx, {"iam": _iam_client()})
        with pytest.raises(RuntimeError, match="overlaps project inventory naming"):
            ownership_cleanup_role._log_cleanup_helper_spec(ctx)


class _HelperHarness:
    """A run whose only owned log group needs the delegated cleanup helper."""

    def __init__(self, state: dict[str, Any] | None = None) -> None:
        self.ctx = _ctx(state if state is not None else _helper_state())
        self.iam = _iam_client()
        self.cfn = MagicMock(name="cloudformation")
        self.sts = MagicMock(name="sts")
        self.sts.get_caller_identity.return_value = {"Account": _ACCOUNT, "Arn": _USER_ARN}
        self.logs = MagicMock(name="logs")
        _route(
            self.ctx,
            {"iam": self.iam, "cloudformation": self.cfn, "sts": self.sts, "logs": self.logs},
        )
        self.spec = ownership_cleanup_role._log_cleanup_helper_spec(self.ctx)
        assert self.spec is not None
        self.stack_id = (
            f"arn:aws:cloudformation:{_REGION}:{_ACCOUNT}:stack/{self.spec['stack_name']}/helper-1"
        )
        self.cfn.get_template.return_value = {"TemplateBody": self.spec["template_body"]}
        self.cfn.create_stack.return_value = {"StackId": self.stack_id}
        self.iam.get_role.return_value = {"Role": self.role()}
        self.iam.list_role_policies.return_value = {
            "PolicyNames": [constants._LOG_CLEANUP_ROLE_POLICY_NAME],
            "IsTruncated": False,
        }
        self.iam.get_role_policy.return_value = {"PolicyDocument": self.spec["role_policy"]}
        self.iam.list_attached_role_policies.return_value = {
            "AttachedPolicies": [],
            "IsTruncated": False,
        }
        self.described: dict[str, list[Any]] = {}
        self.describe_calls: list[str] = []
        self.sleep = MagicMock(name="sleep")

    def stack(self, status: str = "CREATE_COMPLETE", **overrides: Any) -> dict[str, Any]:
        stack = {
            "name": self.spec["stack_name"],
            "stack_id": self.stack_id,
            "status": status,
            "termination_protection": False,
            "tags": {
                constants._LOG_CLEANUP_HELPER_RUN_TAG: _RUN_ID,
                constants._LOG_CLEANUP_HELPER_TOKEN_TAG: _TOKEN,
            },
            "outputs": {constants._LOG_CLEANUP_ROLE_OUTPUT: self.spec["role_arn"]},
        }
        stack.update(overrides)
        return stack

    def role(self, **overrides: Any) -> dict[str, Any]:
        role = {
            "RoleName": self.spec["role_name"],
            "Arn": self.spec["role_arn"],
            "Path": "/",
            "MaxSessionDuration": 3600,
            "AssumeRolePolicyDocument": copy.deepcopy(self.spec["trust_policy"]),
            "Tags": [
                {"Key": constants._LOG_CLEANUP_ROLE_RUN_TAG, "Value": _RUN_ID},
                {"Key": constants._LOG_CLEANUP_ROLE_TOKEN_TAG, "Value": _TOKEN},
            ],
            "RoleId": _ROLE_ID,
            "CreateDate": datetime(2026, 8, 11, 1, 0, tzinfo=UTC),
        }
        role.update(overrides)
        return role

    def record(self, **overrides: Any) -> dict[str, Any]:
        """Persist a prepared helper record, optionally with an active generation."""
        record = ownership_cleanup_role._prepare_log_cleanup_helper_record(self.ctx, self.spec)
        record.update(overrides)
        return record

    def _describe(self, session: Any, region: str, key: str) -> Any:
        assert region == _REGION
        self.describe_calls.append(key)
        responses = self.described.get(key)
        if not responses:
            return None
        if len(responses) > 1:
            return responses.pop(0)
        return responses[0]

    def run(self, action: Callable[[Any], Any]) -> Any:
        with (
            patch_live_validation_helper("describe_stack", side_effect=self._describe),
            patch("time.sleep", self.sleep),
        ):
            return action(self.ctx)


class TestLogCleanupHelperRecord:
    def test_prepared_record_pins_immutable_identity(self) -> None:
        harness = _HelperHarness()
        record = ownership_cleanup_role._prepare_log_cleanup_helper_record(
            harness.ctx, harness.spec
        )
        assert record is harness.ctx.checkpoint.state["log_cleanup_helper"]
        assert record["lifecycle"] == "prepared"
        assert record["active_stack_id"] is None
        assert record["create_sequence"] == 0
        assert record["stack_history"] == []
        assert record["first_caller_arn"] == _USER_ARN
        assert record["template_sha256"] == harness.spec["template_sha256"]
        assert "template_body" not in record

        again = ownership_cleanup_role._prepare_log_cleanup_helper_record(harness.ctx, harness.spec)
        assert again is record

    def test_malformed_or_drifted_record_fails_closed(self) -> None:
        harness = _HelperHarness()
        harness.ctx.checkpoint.state["log_cleanup_helper"] = "not-a-dict"
        with pytest.raises(RuntimeError, match="log_cleanup_helper must be an object"):
            ownership_cleanup_role._prepare_log_cleanup_helper_record(harness.ctx, harness.spec)

        harness = _HelperHarness()
        harness.record(template_sha256="0" * 64)
        with pytest.raises(RuntimeError, match="helper identity changed"):
            ownership_cleanup_role._prepare_log_cleanup_helper_record(harness.ctx, harness.spec)

    def test_stack_generation_is_recorded_once_and_fenced(self) -> None:
        harness = _HelperHarness()
        with pytest.raises(RuntimeError, match="invalid stack ID"):
            ownership_cleanup_role._record_log_cleanup_helper_stack(
                harness.ctx, harness.spec, "arn:aws:cloudformation:us-east-1:1:stack/x/y", "X"
            )

        ownership_cleanup_role._record_log_cleanup_helper_stack(
            harness.ctx, harness.spec, harness.stack_id, "CREATE_IN_PROGRESS"
        )
        ownership_cleanup_role._record_log_cleanup_helper_stack(
            harness.ctx, harness.spec, harness.stack_id, "CREATE_COMPLETE"
        )
        record = harness.ctx.checkpoint.state["log_cleanup_helper"]
        assert record["active_stack_id"] == harness.stack_id
        assert record["lifecycle"] == "CREATE_COMPLETE"
        assert [item["stack_id"] for item in record["stack_history"]] == [harness.stack_id]

        with pytest.raises(RuntimeError, match="generation changed without absence proof"):
            ownership_cleanup_role._record_log_cleanup_helper_stack(
                harness.ctx, harness.spec, f"{harness.stack_id}-2", "CREATE_COMPLETE"
            )

        record["stack_history"] = "not-a-list"
        with pytest.raises(RuntimeError, match="stack_history must be a list"):
            ownership_cleanup_role._record_log_cleanup_helper_stack(
                harness.ctx, harness.spec, harness.stack_id, "CREATE_COMPLETE"
            )

    def test_absence_proof_clears_only_the_matching_generation(self) -> None:
        harness = _HelperHarness()
        ownership_cleanup_role._mark_log_cleanup_helper_absent(harness.ctx, harness.stack_id)
        assert "log_cleanup_helper" not in harness.ctx.checkpoint.state
        harness.ctx.persist_callback.assert_not_called()

        record = harness.record(active_stack_id=harness.stack_id)
        with pytest.raises(RuntimeError, match="refers to a different stack"):
            ownership_cleanup_role._mark_log_cleanup_helper_absent(
                harness.ctx, f"{harness.stack_id}-2"
            )
        assert record["active_stack_id"] == harness.stack_id

        ownership_cleanup_role._mark_log_cleanup_helper_absent(harness.ctx, harness.stack_id)
        assert record["active_stack_id"] is None
        assert record["lifecycle"] == "deleted"
        assert record["last_deleted_stack_id"] == harness.stack_id
        assert record["deleted_at"]

        ownership_cleanup_role._mark_log_cleanup_helper_absent(harness.ctx, None)
        assert record["last_deleted_stack_id"] is None


class TestTemplateDocument:
    def test_only_canonical_json_objects_are_accepted(self) -> None:
        assert ownership_cleanup_role._template_document('{"a": 1}') == {"a": 1}
        assert ownership_cleanup_role._template_document({"a": 1}) == {"a": 1}
        with pytest.raises(RuntimeError, match="not canonical JSON"):
            ownership_cleanup_role._template_document("{not json")
        with pytest.raises(RuntimeError, match="not a JSON object"):
            ownership_cleanup_role._template_document("[1]")
        with pytest.raises(RuntimeError, match="not a JSON object"):
            ownership_cleanup_role._template_document(["Resources"])


class TestValidateLogCleanupHelperStack:
    def test_exact_stack_is_validated_against_the_original_template(self) -> None:
        harness = _HelperHarness()
        assert (
            ownership_cleanup_role._validate_log_cleanup_helper_stack(
                harness.ctx, harness.spec, harness.stack()
            )
            == harness.stack_id
        )
        harness.cfn.get_template.assert_called_once_with(
            StackName=harness.stack_id, TemplateStage="Original"
        )

    @pytest.mark.parametrize(
        ("overrides", "match"),
        [
            ({"name": "other"}, "CloudFormation identity is invalid"),
            (
                {"stack_id": f"arn:aws:cloudformation:{_REGION}:{_ACCOUNT}:stack/other/1"},
                "CloudFormation identity is invalid",
            ),
            ({"termination_protection": True}, "CloudFormation identity is invalid"),
            (
                {"tags": {constants._LOG_CLEANUP_HELPER_RUN_TAG: "other-run"}},
                "CloudFormation tags are invalid",
            ),
            (
                {
                    "tags": {
                        constants._LOG_CLEANUP_HELPER_RUN_TAG: _RUN_ID,
                        constants._LOG_CLEANUP_HELPER_TOKEN_TAG: "b" * 32,
                    }
                },
                "CloudFormation tags are invalid",
            ),
            (
                {
                    "tags": {
                        constants._LOG_CLEANUP_HELPER_RUN_TAG: _RUN_ID,
                        constants._LOG_CLEANUP_HELPER_TOKEN_TAG: _TOKEN,
                        "gco:project": _PROJECT,
                    }
                },
                "CloudFormation tags are invalid",
            ),
            (
                {
                    "tags": {
                        constants._LOG_CLEANUP_HELPER_RUN_TAG: _RUN_ID,
                        constants._LOG_CLEANUP_HELPER_TOKEN_TAG: _TOKEN,
                        "Project": "GCO",
                    }
                },
                "CloudFormation tags are invalid",
            ),
        ],
    )
    def test_identity_or_tag_drift_fails_closed(
        self, overrides: dict[str, Any], match: str
    ) -> None:
        harness = _HelperHarness()
        with pytest.raises(RuntimeError, match=match):
            ownership_cleanup_role._validate_log_cleanup_helper_stack(
                harness.ctx, harness.spec, harness.stack(**overrides)
            )
        harness.cfn.get_template.assert_not_called()

    def test_template_drift_fails_closed(self) -> None:
        harness = _HelperHarness()
        tampered = json.loads(harness.spec["template_body"])
        tampered["Resources"]["CleanupRole"]["Properties"]["MaxSessionDuration"] = 43200
        harness.cfn.get_template.return_value = {"TemplateBody": tampered}
        with pytest.raises(RuntimeError, match="template changed"):
            ownership_cleanup_role._validate_log_cleanup_helper_stack(
                harness.ctx, harness.spec, harness.stack()
            )


class TestValidateLogCleanupHelperRole:
    def _validate(self, harness: _HelperHarness) -> dict[str, str]:
        record = harness.ctx.checkpoint.state["log_cleanup_helper"]
        return ownership_cleanup_role._validate_log_cleanup_helper_role(
            harness.ctx, harness.spec, record, harness.stack_id
        )

    def _harness(self) -> _HelperHarness:
        harness = _HelperHarness()
        ownership_cleanup_role._record_log_cleanup_helper_stack(
            harness.ctx, harness.spec, harness.stack_id, "CREATE_COMPLETE"
        )
        return harness

    def test_exact_role_identity_is_pinned_to_its_stack_generation(self) -> None:
        harness = self._harness()
        identity = self._validate(harness)
        assert identity == {
            "arn": harness.spec["role_arn"],
            "role_id": _ROLE_ID,
            "created_at": "2026-08-11T01:00:00+00:00",
        }
        generation = harness.ctx.checkpoint.state["log_cleanup_helper"]["stack_history"][0]
        assert generation["observed_role_identity"] == identity
        harness.ctx.persist.assert_called_once_with()

        harness.ctx.persist.reset_mock()
        assert self._validate(harness) == identity
        harness.ctx.persist.assert_not_called()

        harness.iam.get_role.return_value = {"Role": harness.role(RoleId="AROAREPLACEMENT")}
        with pytest.raises(RuntimeError, match="role generation changed within its stack"):
            self._validate(harness)

    def test_missing_role_fails_closed(self) -> None:
        harness = self._harness()
        harness.iam.get_role.return_value = {}
        with pytest.raises(RuntimeError, match="omitted the cleanup helper role"):
            self._validate(harness)

    @pytest.mark.parametrize(
        "overrides",
        [
            {"RoleName": "other"},
            {"Arn": f"arn:aws:iam::{_ACCOUNT}:role/other"},
            {"Path": "/service/"},
            {"MaxSessionDuration": 7200},
            {"AssumeRolePolicyDocument": {"Version": "2012-10-17", "Statement": []}},
            {"Tags": [{"Key": constants._LOG_CLEANUP_ROLE_TOKEN_TAG, "Value": _TOKEN}]},
            {
                "Tags": [
                    {"Key": constants._LOG_CLEANUP_ROLE_RUN_TAG, "Value": _RUN_ID},
                    {"Key": constants._LOG_CLEANUP_ROLE_TOKEN_TAG, "Value": "b" * 32},
                    {"Value": "orphan"},
                ]
            },
        ],
    )
    def test_role_identity_drift_fails_closed(self, overrides: dict[str, Any]) -> None:
        harness = self._harness()
        harness.iam.get_role.return_value = {"Role": harness.role(**overrides)}
        with pytest.raises(RuntimeError, match="IAM role identity changed"):
            self._validate(harness)
        harness.iam.list_role_policies.assert_not_called()

    def test_policy_drift_fails_closed(self) -> None:
        harness = self._harness()
        harness.iam.list_role_policies.return_value = {
            "PolicyNames": [constants._LOG_CLEANUP_ROLE_POLICY_NAME],
            "IsTruncated": True,
        }
        with pytest.raises(RuntimeError, match="inline policies changed"):
            self._validate(harness)

        harness.iam.list_role_policies.return_value = {"PolicyNames": ["Extra"]}
        with pytest.raises(RuntimeError, match="inline policies changed"):
            self._validate(harness)

        harness.iam.list_role_policies.return_value = {
            "PolicyNames": [constants._LOG_CLEANUP_ROLE_POLICY_NAME]
        }
        harness.iam.get_role_policy.return_value = {"PolicyDocument": {"Statement": []}}
        with pytest.raises(RuntimeError, match="delete policy changed"):
            self._validate(harness)

        harness.iam.get_role_policy.return_value = {"PolicyDocument": harness.spec["role_policy"]}
        harness.iam.list_attached_role_policies.return_value = {
            "AttachedPolicies": [{"PolicyArn": "arn:aws:iam::aws:policy/AdministratorAccess"}]
        }
        with pytest.raises(RuntimeError, match="gained a managed policy"):
            self._validate(harness)

    @pytest.mark.parametrize("overrides", [{"RoleId": ""}, {"CreateDate": None}])
    def test_immutable_role_identity_must_be_present(self, overrides: dict[str, Any]) -> None:
        harness = self._harness()
        harness.iam.get_role.return_value = {"Role": harness.role(**overrides)}
        with pytest.raises(RuntimeError, match="omitted immutable cleanup role identity"):
            self._validate(harness)

    def test_role_must_map_to_an_exact_recorded_stack_generation(self) -> None:
        harness = self._harness()
        record = harness.ctx.checkpoint.state["log_cleanup_helper"]
        record["stack_history"] = "not-a-list"
        with pytest.raises(RuntimeError, match="stack_history must be a list"):
            self._validate(harness)

        record["stack_history"] = [{"stack_id": f"{harness.stack_id}-other"}, "junk"]
        with pytest.raises(RuntimeError, match="no exact helper stack generation"):
            self._validate(harness)


class TestWaitForLogCleanupHelper:
    def _wait(self, harness: _HelperHarness, *, deleting: bool) -> Any:
        return harness.run(
            lambda ctx: ownership_cleanup_role._wait_for_log_cleanup_helper(
                ctx, harness.spec, harness.stack_id, deleting=deleting
            )
        )

    def test_deletion_completes_on_absence_or_delete_complete(self) -> None:
        harness = _HelperHarness()
        harness.described = {
            harness.stack_id: [
                harness.stack("DELETE_IN_PROGRESS"),
                harness.stack("DELETE_COMPLETE"),
            ]
        }
        assert self._wait(harness, deleting=True) is None
        assert harness.sleep.call_args_list == [((constants._LOG_CLEANUP_STACK_POLL_SECONDS,),)]

        harness = _HelperHarness()
        assert self._wait(harness, deleting=True) is None
        harness.sleep.assert_not_called()

    def test_creation_completes_only_on_create_complete(self) -> None:
        harness = _HelperHarness()
        complete = harness.stack("CREATE_COMPLETE")
        harness.described = {harness.stack_id: [harness.stack("CREATE_IN_PROGRESS"), complete]}
        assert self._wait(harness, deleting=False) == complete
        assert harness.sleep.call_count == 1

    @pytest.mark.parametrize(
        ("status", "deleting", "match"),
        [
            ("DELETE_FAILED", True, "deletion failed"),
            ("DELETE_FAILED", False, "deletion failed"),
            ("ROLLBACK_COMPLETE", False, "creation ended in ROLLBACK_COMPLETE"),
        ],
    )
    def test_terminal_failures_raise(self, status: str, deleting: bool, match: str) -> None:
        harness = _HelperHarness()
        harness.described = {harness.stack_id: [harness.stack(status)]}
        with pytest.raises(RuntimeError, match=match):
            self._wait(harness, deleting=deleting)

    def test_bounded_polling_times_out(self) -> None:
        harness = _HelperHarness()
        harness.described = {harness.stack_id: [harness.stack("CREATE_IN_PROGRESS")]}
        with pytest.raises(RuntimeError, match="creation timed out"):
            self._wait(harness, deleting=False)
        assert harness.sleep.call_count == constants._LOG_CLEANUP_STACK_POLL_ATTEMPTS

        harness = _HelperHarness()
        harness.described = {harness.stack_id: [harness.stack("DELETE_IN_PROGRESS")]}
        with pytest.raises(RuntimeError, match="deletion timed out"):
            self._wait(harness, deleting=True)


class TestCurrentCleanupTrustedPrincipal:
    def test_caller_account_must_match_before_resolving_the_principal(self) -> None:
        harness = _HelperHarness()
        harness.sts.get_caller_identity.return_value = {"Account": "999999999999", "Arn": _USER_ARN}
        with pytest.raises(RuntimeError, match="caller account changed"):
            ownership_cleanup_role._current_cleanup_trusted_principal(harness.ctx)

        harness.sts.get_caller_identity.return_value = {"Account": _ACCOUNT, "Arn": _USER_ARN}
        assert ownership_cleanup_role._current_cleanup_trusted_principal(harness.ctx) == (
            _USER_ARN,
            {"arn": _USER_ARN, "principal_id": _USER_ID},
        )


class TestEnsureLogCleanupHelper:
    @staticmethod
    def _ensure(harness: _HelperHarness) -> dict[str, Any]:
        return harness.run(ownership_cleanup_role._ensure_log_cleanup_helper)

    def _assert_established(self, harness: _HelperHarness, result: dict[str, Any]) -> None:
        assert result == {
            "needed": True,
            "region": _REGION,
            "stack_id": harness.stack_id,
            "stack_name": harness.spec["stack_name"],
            "role_arn": harness.spec["role_arn"],
            "partition": "aws",
            "caller_arn": _USER_ARN,
            "trusted_principal_arn": _USER_ARN,
            "session_policy": harness.spec["role_policy"],
            "external_id": _TOKEN,
        }
        record = harness.ctx.checkpoint.state["log_cleanup_helper"]
        assert record["active_stack_id"] == harness.stack_id
        assert record["lifecycle"] == "CREATE_COMPLETE"
        generation = next(
            item for item in record["stack_history"] if item["stack_id"] == harness.stack_id
        )
        assert generation["observed_role_identity"]["role_id"] == _ROLE_ID

    def test_no_pending_groups_means_no_helper(self) -> None:
        assert ownership_cleanup_role._ensure_log_cleanup_helper(
            _ctx(_stack_state(owned_log_groups=[]))
        ) == {"needed": False}
        assert ownership_cleanup_role._ensure_log_cleanup_helper(
            _ctx(_stack_state(owned_log_groups=[_log_group_record(deleted=True)]))
        ) == {"needed": False}
        with pytest.raises(RuntimeError, match="owned_log_groups must be a list"):
            ownership_cleanup_role._ensure_log_cleanup_helper(
                _ctx(_stack_state(owned_log_groups={}))
            )
        with patch_live_validation_helper("_log_cleanup_helper_spec", return_value=None):
            assert ownership_cleanup_role._ensure_log_cleanup_helper(_ctx(_helper_state())) == {
                "needed": False
            }

    def test_fresh_helper_is_created_validated_and_recorded(self) -> None:
        harness = _HelperHarness()
        harness.described = {
            harness.stack_id: [harness.stack("CREATE_IN_PROGRESS"), harness.stack()],
        }

        result = self._ensure(harness)

        self._assert_established(harness, result)
        record = harness.ctx.checkpoint.state["log_cleanup_helper"]
        assert record["create_sequence"] == 1
        assert record["create_intent_at"]
        create_kwargs = harness.cfn.create_stack.call_args.kwargs
        assert create_kwargs["StackName"] == harness.spec["stack_name"]
        assert create_kwargs["TemplateBody"] == harness.spec["template_body"]
        assert create_kwargs["Capabilities"] == ["CAPABILITY_NAMED_IAM"]
        assert create_kwargs["ClientRequestToken"] == (
            f"live-validation-{harness.spec['stack_name']}-1"
        )
        assert create_kwargs["EnableTerminationProtection"] is False
        assert create_kwargs["OnFailure"] == "ROLLBACK"
        assert create_kwargs["Tags"] == [
            {"Key": constants._LOG_CLEANUP_HELPER_RUN_TAG, "Value": _RUN_ID},
            {"Key": constants._LOG_CLEANUP_HELPER_TOKEN_TAG, "Value": _TOKEN},
        ]
        assert harness.describe_calls == [
            harness.spec["stack_name"],
            harness.stack_id,
            harness.stack_id,
        ]
        assert harness.sleep.call_count == 1

    def test_caller_principal_drift_refuses_to_proceed(self) -> None:
        harness = _HelperHarness()
        harness.sts.get_caller_identity.return_value = {
            "Account": _ACCOUNT,
            "Arn": f"arn:aws:iam::{_ACCOUNT}:user/someone-else",
        }
        harness.iam.get_user.side_effect = lambda UserName: {
            "User": {
                "Arn": f"arn:aws:iam::{_ACCOUNT}:user/{UserName}",
                "UserId": f"AIDA{UserName.upper().replace('-', '')}",
            }
        }
        with pytest.raises(RuntimeError, match="caller principal changed"):
            self._ensure(harness)
        harness.cfn.create_stack.assert_not_called()

    def test_active_complete_generation_is_reused_without_creating(self) -> None:
        harness = _HelperHarness()
        harness.record(
            active_stack_id=harness.stack_id, stack_history=[{"stack_id": harness.stack_id}]
        )
        harness.described = {harness.stack_id: [harness.stack()]}

        result = self._ensure(harness)

        self._assert_established(harness, result)
        harness.cfn.create_stack.assert_not_called()

    def test_deleted_active_generation_is_marked_absent_and_recreated(self) -> None:
        harness = _HelperHarness()
        old_stack_id = f"{harness.stack_id}-old"
        harness.record(active_stack_id=old_stack_id, stack_history=[{"stack_id": old_stack_id}])
        harness.described = {
            old_stack_id: [harness.stack("DELETE_COMPLETE", stack_id=old_stack_id)],
            harness.stack_id: [harness.stack()],
        }

        result = self._ensure(harness)

        self._assert_established(harness, result)
        harness.cfn.create_stack.assert_called_once()
        record = harness.ctx.checkpoint.state["log_cleanup_helper"]
        assert record["last_deleted_stack_id"] == old_stack_id
        assert [item["stack_id"] for item in record["stack_history"]] == [
            old_stack_id,
            harness.stack_id,
        ]

    def test_named_stack_without_checkpointed_generation_is_adopted_after_validation(self) -> None:
        harness = _HelperHarness()
        harness.described = {
            harness.spec["stack_name"]: [harness.stack("CREATE_IN_PROGRESS")],
            harness.stack_id: [harness.stack()],
        }

        result = self._ensure(harness)

        self._assert_established(harness, result)
        harness.cfn.create_stack.assert_not_called()

    def test_named_stack_replacing_the_active_generation_is_refused(self) -> None:
        harness = _HelperHarness()
        old_stack_id = f"{harness.stack_id}-old"
        harness.record(active_stack_id=old_stack_id, stack_history=[{"stack_id": old_stack_id}])
        harness.described = {harness.spec["stack_name"]: [harness.stack()]}
        with pytest.raises(
            RuntimeError, match="different cleanup helper stack generation appeared"
        ):
            self._ensure(harness)

    def test_deleting_generation_is_awaited_before_recreation(self) -> None:
        harness = _HelperHarness()
        old_stack_id = f"{harness.stack_id}-old"
        harness.record(active_stack_id=old_stack_id, stack_history=[{"stack_id": old_stack_id}])
        harness.described = {
            old_stack_id: [
                harness.stack("DELETE_IN_PROGRESS", stack_id=old_stack_id),
                harness.stack("DELETE_COMPLETE", stack_id=old_stack_id),
            ],
            harness.stack_id: [harness.stack()],
        }

        result = self._ensure(harness)

        self._assert_established(harness, result)
        assert harness.ctx.checkpoint.state["log_cleanup_helper"]["last_deleted_stack_id"] == (
            old_stack_id
        )
        harness.cfn.create_stack.assert_called_once()

    def test_already_exists_race_adopts_the_described_stack(self) -> None:
        harness = _HelperHarness()
        harness.cfn.create_stack.side_effect = _client_error("AlreadyExistsException")
        harness.described = {
            harness.spec["stack_name"]: [None, harness.stack("CREATE_IN_PROGRESS")],
            harness.stack_id: [harness.stack()],
        }

        result = self._ensure(harness)

        self._assert_established(harness, result)

    def test_already_exists_without_a_describable_stack_fails_closed(self) -> None:
        harness = _HelperHarness()
        harness.cfn.create_stack.side_effect = _client_error("AlreadyExistsException")
        with pytest.raises(RuntimeError, match="exists but cannot be described"):
            self._ensure(harness)

    def test_other_create_errors_propagate(self) -> None:
        harness = _HelperHarness()
        harness.cfn.create_stack.side_effect = _client_error("InsufficientCapabilitiesException")
        with pytest.raises(ClientError):
            self._ensure(harness)

    def test_stack_vanishing_after_creation_fails_closed(self) -> None:
        harness = _HelperHarness()
        with (
            patch_live_validation_helper("_wait_for_log_cleanup_helper", return_value=None),
            pytest.raises(RuntimeError, match="disappeared after creation"),
        ):
            self._ensure(harness)

    def test_role_output_drift_fails_closed(self) -> None:
        harness = _HelperHarness()
        harness.described = {
            harness.stack_id: [
                harness.stack(
                    outputs={constants._LOG_CLEANUP_ROLE_OUTPUT: f"arn:aws:iam::{_ACCOUNT}:role/x"}
                )
            ]
        }
        with pytest.raises(RuntimeError, match="role output changed"):
            self._ensure(harness)
        harness.iam.get_role.assert_not_called()


class TestDeleteLogCleanupHelper:
    @staticmethod
    def _delete(harness: _HelperHarness) -> dict[str, Any]:
        return harness.run(ownership_cleanup_role._delete_log_cleanup_helper)

    def test_no_helper_record_means_nothing_to_delete(self) -> None:
        assert ownership_cleanup_role._delete_log_cleanup_helper(_ctx(_helper_state())) == {
            "needed": False,
            "deleted": True,
        }
        with pytest.raises(RuntimeError, match="log_cleanup_helper must be an object"):
            ownership_cleanup_role._delete_log_cleanup_helper(
                _ctx(_helper_state(log_cleanup_helper="x"))
            )

    def test_helper_without_authority_records_fails_closed(self) -> None:
        harness = _HelperHarness()
        harness.record()
        harness.ctx.checkpoint.state["owned_log_groups"] = []
        with pytest.raises(RuntimeError, match="without log-group authority records"):
            self._delete(harness)

    def test_absent_generation_is_marked_without_a_delete_call(self) -> None:
        harness = _HelperHarness()
        harness.record(active_stack_id=harness.stack_id)
        harness.described = {harness.stack_id: [harness.stack("DELETE_COMPLETE")]}

        assert self._delete(harness) == {
            "needed": True,
            "deleted": True,
            "already_absent": True,
            "stack_id": harness.stack_id,
        }
        harness.cfn.delete_stack.assert_not_called()
        record = harness.ctx.checkpoint.state["log_cleanup_helper"]
        assert record["lifecycle"] == "deleted"
        assert record["last_deleted_stack_id"] == harness.stack_id

        harness = _HelperHarness()
        harness.record()
        assert self._delete(harness) == {
            "needed": False,
            "deleted": True,
            "already_absent": True,
            "stack_id": None,
        }

    def test_live_generation_is_deleted_and_proven_absent(self) -> None:
        harness = _HelperHarness()
        harness.record(
            active_stack_id=harness.stack_id, stack_history=[{"stack_id": harness.stack_id}]
        )
        harness.described = {harness.stack_id: [harness.stack(), harness.stack("DELETE_COMPLETE")]}

        assert self._delete(harness) == {
            "needed": True,
            "deleted": True,
            "stack_id": harness.stack_id,
        }
        harness.cfn.delete_stack.assert_called_once_with(
            StackName=harness.stack_id,
            ClientRequestToken=f"delete-{harness.spec['stack_name']}-helper-1",
        )
        harness.iam.get_role.assert_called_once()
        record = harness.ctx.checkpoint.state["log_cleanup_helper"]
        assert record["delete_intent_at"]
        assert record["lifecycle"] == "deleted"
        assert record["active_stack_id"] is None
        harness.ctx.persist.assert_called()

    def test_named_generation_without_checkpoint_is_validated_then_deleted(self) -> None:
        harness = _HelperHarness()
        harness.record()
        harness.described = {
            harness.spec["stack_name"]: [harness.stack("ROLLBACK_COMPLETE"), None],
        }

        result = self._delete(harness)

        assert result["deleted"] is True
        harness.iam.get_role.assert_not_called()
        harness.cfn.delete_stack.assert_called_once()

    def test_replacement_generation_is_never_deleted(self) -> None:
        harness = _HelperHarness()
        harness.record(active_stack_id=f"{harness.stack_id}-old")
        harness.described = {harness.spec["stack_name"]: [harness.stack()]}
        with pytest.raises(RuntimeError, match="Refusing to delete a replacement"):
            self._delete(harness)
        harness.cfn.delete_stack.assert_not_called()

    def test_in_progress_deletion_is_awaited_not_reissued(self) -> None:
        harness = _HelperHarness()
        harness.record(
            active_stack_id=harness.stack_id, stack_history=[{"stack_id": harness.stack_id}]
        )
        harness.described = {
            harness.stack_id: [
                harness.stack("DELETE_IN_PROGRESS"),
                harness.stack("DELETE_IN_PROGRESS"),
                None,
            ]
        }

        assert self._delete(harness)["deleted"] is True
        harness.cfn.delete_stack.assert_not_called()
        assert harness.sleep.call_count == 1

    def test_same_name_replacement_during_deletion_fails_closed(self) -> None:
        harness = _HelperHarness()
        harness.record(
            active_stack_id=harness.stack_id, stack_history=[{"stack_id": harness.stack_id}]
        )
        harness.described = {
            harness.stack_id: [harness.stack(), None],
            harness.spec["stack_name"]: [harness.stack(stack_id=f"{harness.stack_id}-2")],
        }
        with pytest.raises(RuntimeError, match="replaced during deletion"):
            self._delete(harness)


class TestTagConditionedLogDeleter:
    def _helper(self, harness: _HelperHarness) -> dict[str, Any]:
        return {
            "needed": True,
            "region": _REGION,
            "stack_id": harness.stack_id,
            "role_arn": harness.spec["role_arn"],
            "partition": "aws",
            "external_id": _TOKEN,
            "session_policy": harness.spec["role_policy"],
        }

    def _assume_role(self, harness: _HelperHarness, **overrides: Any) -> Callable[..., Any]:
        def assume_role(**kwargs: Any) -> dict[str, Any]:
            response = {
                "Credentials": {
                    "AccessKeyId": "AKIA",
                    "SecretAccessKey": "secret",
                    "SessionToken": "token",
                    "Expiration": datetime(2026, 8, 11, 2, 0, tzinfo=UTC),
                },
                "AssumedRoleUser": {
                    "Arn": (
                        f"arn:aws:sts::{_ACCOUNT}:assumed-role/{harness.spec['role_name']}/"
                        f"{kwargs['RoleSessionName']}"
                    )
                },
            }
            response.update(overrides)
            return response

        return assume_role

    def test_session_is_established_once_and_clients_cached_per_region(self) -> None:
        harness = _HelperHarness()
        harness.sts.assume_role.side_effect = self._assume_role(harness)
        restricted: dict[str, MagicMock] = {}

        def client(service: str, **kwargs: Any) -> Any:
            if service == "sts":
                return harness.sts
            assert service == "logs"
            assert kwargs["aws_access_key_id"] == "AKIA"
            assert kwargs["aws_secret_access_key"] == "secret"
            assert kwargs["aws_session_token"] == "token"
            return restricted.setdefault(
                kwargs["region_name"], MagicMock(name=kwargs["region_name"])
            )

        harness.ctx.session.client.side_effect = client
        deleter = ownership_cleanup_role.TagConditionedLogDeleter(harness.ctx)
        assert deleter.authorization == {"needed": False}

        with patch_live_validation_helper(
            "_ensure_log_cleanup_helper", return_value=self._helper(harness)
        ) as ensure:
            first = deleter.client(_REGION)
            assert deleter.client(_REGION) is first
            other = deleter.client(_OTHER_REGION)

        assert other is not first
        ensure.assert_called_once_with(harness.ctx)
        harness.sts.assume_role.assert_called_once()
        assume_kwargs = harness.sts.assume_role.call_args.kwargs
        session_name = (
            "live-validation-logs-"
            + uuid.uuid5(constants._LOG_CLEANUP_HELPER_NAMESPACE, _RUN_ID).hex[:16]
        )
        assert assume_kwargs == {
            "RoleArn": harness.spec["role_arn"],
            "RoleSessionName": session_name,
            "DurationSeconds": constants._LOG_CLEANUP_SESSION_SECONDS,
            "ExternalId": _TOKEN,
            "Policy": json.dumps(
                harness.spec["role_policy"], separators=(",", ":"), sort_keys=True
            ),
        }
        assert deleter.authorization == {
            "needed": True,
            "mode": "sts-assume-role-session-policy",
            "role_arn": harness.spec["role_arn"],
            "helper_stack_id": harness.stack_id,
            "atomic_resource_tag_condition": True,
            "condition_tag_keys": [constants._RUN_STACK_TAG, constants._LOG_CLEANUP_TOKEN_TAG],
            "session_expiration": "2026-08-11T02:00:00+00:00",
        }

    def test_missing_expiration_is_reported_as_none(self) -> None:
        harness = _HelperHarness()
        harness.sts.assume_role.side_effect = self._assume_role(harness)
        original = self._assume_role(harness)

        def assume_role(**kwargs: Any) -> dict[str, Any]:
            response = original(**kwargs)
            del response["Credentials"]["Expiration"]
            return response

        harness.sts.assume_role.side_effect = assume_role
        deleter = ownership_cleanup_role.TagConditionedLogDeleter(harness.ctx)
        with patch_live_validation_helper(
            "_ensure_log_cleanup_helper", return_value=self._helper(harness)
        ):
            deleter.client(_REGION)
        assert deleter.authorization["session_expiration"] is None

    def test_helper_that_is_not_needed_cannot_hand_out_clients(self) -> None:
        harness = _HelperHarness()
        deleter = ownership_cleanup_role.TagConditionedLogDeleter(harness.ctx)
        with (
            patch_live_validation_helper(
                "_ensure_log_cleanup_helper", return_value={"needed": False}
            ),
            pytest.raises(RuntimeError, match="role was not created for pending groups"),
        ):
            deleter.client(_REGION)
        harness.sts.assume_role.assert_not_called()

    def test_incomplete_or_foreign_session_is_refused(self) -> None:
        harness = _HelperHarness()
        deleter = ownership_cleanup_role.TagConditionedLogDeleter(harness.ctx)
        with patch_live_validation_helper(
            "_ensure_log_cleanup_helper", return_value=self._helper(harness)
        ):
            harness.sts.assume_role.side_effect = self._assume_role(
                harness, Credentials={"AccessKeyId": "AKIA", "SecretAccessKey": "secret"}
            )
            with pytest.raises(RuntimeError, match="omitted cleanup session credentials"):
                deleter.client(_REGION)

            harness.sts.assume_role.side_effect = self._assume_role(
                harness,
                AssumedRoleUser={"Arn": f"arn:aws:sts::{_ACCOUNT}:assumed-role/Other/session"},
            )
            with pytest.raises(RuntimeError, match="unexpected cleanup principal"):
                deleter.client(_REGION)
        assert deleter.authorization == {"needed": False}

    def test_session_that_yields_no_credentials_fails_closed(self) -> None:
        deleter = ownership_cleanup_role.TagConditionedLogDeleter(_ctx(_helper_state()))
        with (
            patch.object(
                ownership_cleanup_role.TagConditionedLogDeleter,
                "_establish_session",
                lambda self: None,
            ),
            pytest.raises(RuntimeError, match="session was not established"),
        ):
            deleter.client(_REGION)


# ---------------------------------------------------------------------------
# ownership/log_groups.py
# ---------------------------------------------------------------------------

_CLUSTER = f"{_PROJECT}-cluster"
_CLUSTER_ARN = f"arn:aws:eks:{_REGION}:{_ACCOUNT}:cluster/{_CLUSTER}"
_FUNCTION = f"{_STACK}-Worker-ABC"


def _eks_identity(status: str = "ACTIVE") -> dict[str, str]:
    return {"name": _CLUSTER, "arn": _CLUSTER_ARN, "status": status}


class TestDerivedLogGroupNames:
    def test_each_source_type_derives_its_exact_group_names(self) -> None:
        derive = ownership_log_groups._derived_log_group_names
        assert derive("AWS::Logs::LogGroup", _LOG_GROUP) == (_LOG_GROUP,)
        assert derive("AWS::Lambda::Function", _FUNCTION) == (f"/aws/lambda/{_FUNCTION}",)
        assert derive("AWS::EKS::Cluster", _CLUSTER) == (
            f"/aws/eks/{_CLUSTER}/cluster",
            f"/aws/containerinsights/{_CLUSTER}/application",
            f"/aws/containerinsights/{_CLUSTER}/dataplane",
            f"/aws/containerinsights/{_CLUSTER}/host",
            f"/aws/containerinsights/{_CLUSTER}/performance",
        )
        assert derive("AWS::S3::Bucket", "bucket") == ()


class TestEksClusterLogAuthorityIdentity:
    def _ctx(self, cluster: Any, *, error: ClientError | None = None) -> tuple[Any, MagicMock]:
        ctx = _ctx()
        eks = MagicMock(name="eks")
        if error is not None:
            eks.describe_cluster.side_effect = error
        else:
            eks.describe_cluster.return_value = {"cluster": cluster} if cluster is not None else {}
        _route(ctx, {"eks": eks})
        return ctx, eks

    def test_live_identity_requires_the_exact_active_cluster(self) -> None:
        ctx, eks = self._ctx(_eks_identity())
        assert ownership_log_groups._live_eks_cluster_identity(ctx, _REGION, _CLUSTER) == (
            _eks_identity()
        )
        eks.describe_cluster.assert_called_once_with(name=_CLUSTER)

        ctx, _eks = self._ctx(None)
        with pytest.raises(RuntimeError, match="omitted cluster identity"):
            ownership_log_groups._live_eks_cluster_identity(ctx, _REGION, _CLUSTER)

        ctx, _eks = self._ctx(_eks_identity("CREATING"))
        with pytest.raises(RuntimeError, match="not exact and ACTIVE"):
            ownership_log_groups._live_eks_cluster_identity(ctx, _REGION, _CLUSTER)

        ctx, _eks = self._ctx({**_eks_identity(), "arn": f"{_CLUSTER_ARN}-2"})
        with pytest.raises(RuntimeError, match="not exact and ACTIVE"):
            ownership_log_groups._live_eks_cluster_identity(ctx, _REGION, _CLUSTER)

        ctx, eks = self._ctx(_eks_identity())
        ctx.session.get_partition_for_region.return_value = None
        with pytest.raises(RuntimeError, match="Could not resolve AWS partition"):
            ownership_log_groups._live_eks_cluster_identity(ctx, _REGION, _CLUSTER)
        eks.describe_cluster.assert_not_called()

    def test_deleted_tombstone_is_only_derived_when_rollback_allows_it(self) -> None:
        not_found = _client_error("ResourceNotFoundException", "DescribeCluster")
        ctx, _eks = self._ctx(None, error=not_found)
        with pytest.raises(ClientError):
            ownership_log_groups._eks_cluster_log_authority_identity(
                ctx, _REGION, _CLUSTER, allow_deleted=False
            )
        assert ownership_log_groups._eks_cluster_log_authority_identity(
            ctx, _REGION, _CLUSTER, allow_deleted=True
        ) == _eks_identity("DELETED")

        ctx, _eks = self._ctx(_eks_identity())
        assert (
            ownership_log_groups._eks_cluster_log_authority_identity(
                ctx, _REGION, _CLUSTER, allow_deleted=True
            )
            == _eks_identity()
        )

        ctx, _eks = self._ctx(None, error=_client_error("AccessDeniedException"))
        with pytest.raises(ClientError):
            ownership_log_groups._eks_cluster_log_authority_identity(
                ctx, _REGION, _CLUSTER, allow_deleted=True
            )

        ctx, _eks = self._ctx(_eks_identity())
        ctx.session.get_partition_for_region.return_value = None
        with pytest.raises(RuntimeError, match="Could not resolve AWS partition"):
            ownership_log_groups._eks_cluster_log_authority_identity(
                ctx, _REGION, _CLUSTER, allow_deleted=True
            )


class TestValidatedOwnedLogGroupIdentity:
    def test_exact_stack_derived_records_are_accepted(self) -> None:
        validate = ownership_log_groups._validated_owned_log_group_identity
        assert validate(_ctx(), _log_group_record()) == (_REGION, _LOG_GROUP)
        assert validate(
            _ctx(),
            _log_group_record(
                name=f"/aws/lambda/{_FUNCTION}",
                source_resource_type="AWS::Lambda::Function",
                source_physical_id=_FUNCTION,
            ),
        ) == (_REGION, f"/aws/lambda/{_FUNCTION}")
        for status in ("ACTIVE", "DELETED"):
            assert validate(
                _ctx(),
                _log_group_record(
                    name=f"/aws/containerinsights/{_CLUSTER}/host",
                    source_resource_type="AWS::EKS::Cluster",
                    source_physical_id=_CLUSTER,
                    source_service_identity=_eks_identity(status),
                ),
            ) == (_REGION, f"/aws/containerinsights/{_CLUSTER}/host")

    @pytest.mark.parametrize(
        ("state_override", "record_override", "match"),
        [
            ({"target_stack_regions": []}, {}, "target stack is invalid"),
            ({"target_stack_regions": {_STACK: _OTHER_REGION}}, {}, "target stack is invalid"),
            ({}, {"name": ""}, "source is invalid"),
            ({}, {"source_logical_id": ""}, "source is invalid"),
            ({}, {"source_resource_type": "AWS::S3::Bucket"}, "source is invalid"),
            ({}, {"source_physical_id": "other-group"}, "source is invalid"),
            (
                {},
                {
                    "name": f"/aws/eks/{_CLUSTER}/cluster",
                    "source_resource_type": "AWS::EKS::Cluster",
                    "source_physical_id": _CLUSTER,
                    "source_service_identity": _eks_identity("CREATING"),
                },
                "lacks exact live EKS identity",
            ),
            (
                {},
                {
                    "name": f"/aws/eks/{_CLUSTER}/cluster",
                    "source_resource_type": "AWS::EKS::Cluster",
                    "source_physical_id": _CLUSTER,
                },
                "lacks exact live EKS identity",
            ),
            ({}, {"source_service_identity": _eks_identity()}, "Unexpected service identity"),
            ({"owned_stacks": {}}, {}, "authority is invalid"),
            ({}, {"stack_id": f"{_STACK_ID}-2"}, "authority is invalid"),
            (
                {"owned_stacks": {_REGION: {_STACK: _owned_stack(run_tag="other-run")}}},
                {},
                "authority is invalid",
            ),
            ({}, {"run_tag": "other-run"}, "authority is invalid"),
            ({}, {"ownership_authority": "name-match"}, "authority is invalid"),
            ({}, {"authority_phase": "post-destroy"}, "authority is invalid"),
            ({}, {"cleanup_token": ""}, "authority is invalid"),
            ({}, {"cleanup_token": "b" * 32}, "authority is invalid"),
        ],
    )
    def test_identity_or_authority_drift_fails_closed(
        self, state_override: dict[str, Any], record_override: dict[str, Any], match: str
    ) -> None:
        ctx = _ctx(_stack_state(**state_override))
        with pytest.raises(RuntimeError, match=match):
            ownership_log_groups._validated_owned_log_group_identity(
                ctx, _log_group_record(**record_override)
            )

    def test_unresolvable_partition_fails_closed(self) -> None:
        with pytest.raises(RuntimeError, match="Could not resolve AWS partition"):
            ownership_log_groups._validated_owned_log_group_identity(
                _ctx(partition=None), _log_group_record()
            )


class TestLogGroupIdentityReads:
    def test_exact_name_is_selected_across_prefix_matches_and_pages(self) -> None:
        client = MagicMock(name="logs")
        client.describe_log_groups.side_effect = [
            {"logGroups": [{"logGroupName": f"{_LOG_GROUP}-suffix"}], "nextToken": "t1"},
            {"logGroups": [{"logGroupName": _LOG_GROUP, "creationTime": 5}]},
        ]
        assert ownership_log_groups._describe_exact_log_group(client, _LOG_GROUP) == {
            "logGroupName": _LOG_GROUP,
            "creationTime": 5,
        }
        assert [call.kwargs for call in client.describe_log_groups.call_args_list] == [
            {"logGroupNamePrefix": _LOG_GROUP, "limit": 50},
            {"logGroupNamePrefix": _LOG_GROUP, "limit": 50, "nextToken": "t1"},
        ]

        client.describe_log_groups.side_effect = None
        client.describe_log_groups.return_value = {"logGroups": []}
        assert ownership_log_groups._describe_exact_log_group(client, _LOG_GROUP) is None

        client.describe_log_groups.return_value = {"logGroups": ["not-an-object"]}
        with pytest.raises(RuntimeError, match="non-object log-group record"):
            ownership_log_groups._describe_exact_log_group(client, _LOG_GROUP)

    def test_identity_requires_arn_and_creation_time(self) -> None:
        client = MagicMock(name="logs")
        client.list_tags_for_resource.return_value = {"tags": {"k": "v"}}

        client.describe_log_groups.return_value = {"logGroups": []}
        assert ownership_log_groups._log_group_identity(client, _REGION, _LOG_GROUP) is None

        client.describe_log_groups.return_value = {
            "logGroups": [
                {"logGroupName": _LOG_GROUP, "arn": f"{_LOG_GROUP_ARN}:*", "creationTime": 7}
            ]
        }
        assert ownership_log_groups._log_group_identity(client, _REGION, _LOG_GROUP) == {
            "arn": _LOG_GROUP_ARN,
            "creation_time": 7,
            "tags": {"k": "v"},
        }
        client.list_tags_for_resource.assert_called_once_with(resourceArn=_LOG_GROUP_ARN)

        client.list_tags_for_resource.return_value = {}
        client.describe_log_groups.return_value = {
            "logGroups": [
                {"logGroupName": _LOG_GROUP, "logGroupArn": _LOG_GROUP_ARN, "creationTime": 7}
            ]
        }
        assert ownership_log_groups._log_group_identity(client, _REGION, _LOG_GROUP)["tags"] == {}

        for broken in (
            {"logGroupName": _LOG_GROUP, "creationTime": 7},
            {"logGroupName": _LOG_GROUP, "arn": _LOG_GROUP_ARN, "creationTime": "7"},
        ):
            client.describe_log_groups.return_value = {"logGroups": [broken]}
            with pytest.raises(RuntimeError, match="omitted identity"):
                ownership_log_groups._log_group_identity(client, _REGION, _LOG_GROUP)

    def test_generation_is_the_immutable_arn_and_creation_time(self) -> None:
        assert ownership_log_groups._log_group_generation(_identity(9)) == {
            "arn": _LOG_GROUP_ARN,
            "creation_time": 9,
        }
        with pytest.raises(RuntimeError, match="identity is malformed"):
            ownership_log_groups._log_group_generation({"arn": _LOG_GROUP_ARN})
        with pytest.raises(RuntimeError, match="identity is malformed"):
            ownership_log_groups._log_group_generation({"creation_time": 9})


class TestObserveLogGroupStability:
    def _observe(
        self,
        reads: list[Any],
        *,
        expected_identity: Mapping[str, Any] | None = None,
        expected_tags: Mapping[str, str] | None = None,
        required_present: int | None = 2,
        required_absent: int | None = 2,
        attempts: int = 6,
    ) -> tuple[dict[str, Any], MagicMock]:
        sleep = MagicMock()
        with (
            patch_live_validation_helper("_log_group_identity", side_effect=reads),
            patch("time.sleep", sleep),
        ):
            outcome = ownership_log_groups._observe_log_group_stability(
                MagicMock(),
                _REGION,
                _LOG_GROUP,
                expected_identity=expected_identity,
                expected_tags=expected_tags,
                required_present=required_present,
                required_absent=required_absent,
                attempts=attempts,
                poll_seconds=0.5,
            )
        return outcome, sleep

    @pytest.mark.parametrize(
        ("kwargs", "match"),
        [
            ({"attempts": True}, "attempts must be a positive integer"),
            ({"attempts": "6"}, "attempts must be a positive integer"),
            ({"attempts": 0}, "attempts must be a positive integer"),
            ({"required_present": 0}, "required_present must be a positive integer"),
            ({"required_absent": -1}, "required_absent must be a positive integer"),
            ({"required_present": None, "required_absent": None}, "At least one stable"),
            ({"poll_seconds": -1}, "poll_seconds must be non-negative"),
        ],
    )
    def test_bounds_are_validated_before_any_read(self, kwargs: dict[str, Any], match: str) -> None:
        arguments: dict[str, Any] = {"required_present": 2, "required_absent": 2}
        arguments.update(kwargs)
        with (
            patch_live_validation_helper("_log_group_identity") as reader,
            pytest.raises(ValueError, match=match),
        ):
            ownership_log_groups._observe_log_group_stability(
                MagicMock(), _REGION, _LOG_GROUP, **arguments
            )
        reader.assert_not_called()

    def test_stable_presence_and_absence_are_reported_with_streaks(self) -> None:
        identity = _identity()
        outcome, sleep = self._observe([identity, identity])
        assert outcome["status"] == "present"
        assert outcome["consecutive"] == 2
        assert outcome["identity"] == identity
        assert [item["status"] for item in outcome["observations"]] == ["present", "present"]
        assert sleep.call_args_list == [((0.5,),)]

        outcome, _sleep = self._observe([None, None])
        assert outcome["status"] == "absent"
        assert outcome["consecutive"] == 2
        assert outcome["identity"] is None

    def test_non_retryable_errors_propagate(self) -> None:
        with pytest.raises(ClientError):
            self._observe([_client_error("AccessDeniedException", "DescribeLogGroups")])

    def test_retryable_errors_reset_the_streak(self) -> None:
        identity = _identity()
        outcome, _sleep = self._observe(
            [
                identity,
                _client_error("ThrottlingException", "DescribeLogGroups"),
                identity,
                identity,
            ]
        )
        assert outcome["status"] == "present"
        assert outcome["attempt_count"] == 4
        assert outcome["observations"][1]["status"] == "retryable-error"
        assert outcome["observations"][1]["error_code"] == "ThrottlingException"

    def test_disappearance_after_a_seen_generation_is_a_replacement(self) -> None:
        identity = _identity()
        outcome, _sleep = self._observe([identity, None], required_present=3, required_absent=3)
        assert outcome["status"] == "replacement"
        assert outcome["expected_generation"] == {
            "arn": _LOG_GROUP_ARN,
            "creation_time": identity["creation_time"],
        }
        assert outcome["observed_generation"] is None
        assert outcome["observations"][-1]["status"] == "replacement"

    def test_second_generation_without_an_expectation_is_a_replacement(self) -> None:
        first = _identity(_RUN_STARTED_MS + 1)
        second = _identity(_RUN_STARTED_MS + 2)
        outcome, _sleep = self._observe([first, second], required_present=3)
        assert outcome["status"] == "replacement"
        assert outcome["expected_generation"]["creation_time"] == first["creation_time"]
        assert outcome["observed_generation"]["creation_time"] == second["creation_time"]

    def test_expected_generation_replacement_needs_consecutive_reads(self) -> None:
        expected = _identity(_RUN_STARTED_MS + 1)
        other = _identity(_RUN_STARTED_MS + 2)
        third = _identity(_RUN_STARTED_MS + 3)

        outcome, _sleep = self._observe([other, other], expected_identity=expected)
        assert outcome["status"] == "replacement"
        assert outcome["consecutive"] == 2
        assert outcome["observed_generation"]["creation_time"] == other["creation_time"]
        assert [item["status"] for item in outcome["observations"]] == [
            "replacement-candidate",
            "replacement",
        ]

        outcome, sleep = self._observe(
            [other, third, expected], expected_identity=expected, attempts=3, required_present=3
        )
        assert outcome["status"] == "unsettled"
        assert outcome["present_streak"] == 1
        assert [item.get("consecutive") for item in outcome["observations"]] == [1, 1, 1]
        assert sleep.call_count == 2

        outcome, sleep = self._observe([expected, other], expected_identity=expected, attempts=2)
        assert outcome["status"] == "unsettled"
        assert outcome["observations"][-1]["status"] == "replacement-candidate"
        assert sleep.call_count == 1

    def test_authority_tag_drift_stops_observation(self) -> None:
        drifted = _identity(tags={constants._RUN_STACK_TAG: "other-run"})
        outcome, _sleep = self._observe([drifted, drifted], expected_tags=_AUTHORITY_TAGS)
        assert outcome["status"] == "tag-drift"
        assert outcome["tag_drift"] == {
            constants._RUN_STACK_TAG: {"expected": _RUN_ID, "observed": "other-run"},
            constants._LOG_CLEANUP_TOKEN_TAG: {"expected": _TOKEN, "observed": None},
        }
        assert outcome["attempt_count"] == 1


class TestLogGroupEvidenceRecording:
    def test_observation_history_is_bounded_and_replacements_are_kept(self) -> None:
        ctx = _ctx()
        record = _log_group_record(
            identity_observation_history=[{"phase": "old"}]
            * constants._LOG_GROUP_OBSERVATION_HISTORY_LIMIT
        )
        entry = ownership_log_groups._record_log_group_observation(
            ctx, record, phase="pending-delete", outcome={"status": "replacement", "x": 1}
        )
        assert entry["phase"] == "pending-delete"
        assert entry["status"] == "replacement"
        history = record["identity_observation_history"]
        assert len(history) == constants._LOG_GROUP_OBSERVATION_HISTORY_LIMIT
        assert history[-1] == entry
        assert record["replacement_evidence"] == [entry]
        ctx.persist_callback.assert_called_once_with(ctx.checkpoint)

    def test_malformed_evidence_lists_fail_closed(self) -> None:
        ctx = _ctx()
        with pytest.raises(RuntimeError, match="identity_observation_history must be a list"):
            ownership_log_groups._record_log_group_observation(
                ctx,
                _log_group_record(identity_observation_history={}),
                phase="p",
                outcome={"status": "present"},
            )
        with pytest.raises(RuntimeError, match="replacement_evidence must be a list"):
            ownership_log_groups._record_log_group_observation(
                ctx,
                _log_group_record(replacement_evidence={}),
                phase="p",
                outcome={"status": "replacement"},
            )
        with pytest.raises(RuntimeError, match="log_group_checkpoint_incidents must be a list"):
            ownership_log_groups._record_log_group_checkpoint_incident(
                _ctx(_stack_state(log_group_checkpoint_incidents={})),
                _log_group_record(),
                phase="p",
                outcome={"status": "absent"},
            )

    def test_incidents_and_dispositions_are_persisted(self) -> None:
        ctx = _ctx()
        ownership_log_groups._record_log_group_checkpoint_incident(
            ctx, _log_group_record(), phase="checkpoint-create-race", outcome={"status": "absent"}
        )
        incident = ctx.checkpoint.state["log_group_checkpoint_incidents"][0]
        assert incident["phase"] == "checkpoint-create-race"
        assert incident["candidate"] == _log_group_record()
        assert incident["outcome"] == {"status": "absent"}

        record = _log_group_record(observed_identity=_identity())
        disposition = ownership_log_groups._set_log_group_disposition(
            ctx, record, status="deleted", phase="cleanup", outcome={"status": "absent"}
        )
        assert record["original_generation_disposition"] is disposition
        assert disposition["status"] == "deleted"
        assert disposition["original_identity"] == _identity()
        assert disposition["last_observation_status"] == "absent"
        assert ctx.persist_callback.call_count == 2


def _resource(
    resource_type: str,
    logical_id: str,
    physical_id: str,
    status: str = "CREATE_COMPLETE",
) -> dict[str, str]:
    return {
        "ResourceType": resource_type,
        "LogicalResourceId": logical_id,
        "PhysicalResourceId": physical_id,
        "ResourceStatus": status,
    }


def _present(identity: Mapping[str, Any] | None = "default") -> dict[str, Any]:
    outcome: dict[str, Any] = {"status": "present"}
    if identity == "default":
        outcome["identity"] = _identity()
    elif identity is not None:
        outcome["identity"] = dict(identity)
    return outcome


def _absent() -> dict[str, Any]:
    return {"status": "absent"}


class _CheckpointHarness:
    """One owned regional stack whose log-source resources are checkpointed."""

    def __init__(
        self,
        resources: list[dict[str, Any]] | None = None,
        *,
        stack_status: str = "CREATE_COMPLETE",
        live_stack: Any = "default",
        state: dict[str, Any] | None = None,
    ) -> None:
        self.ctx = _ctx(state if state is not None else _stack_state())
        summaries = (
            resources
            if resources is not None
            else [_resource("AWS::Logs::LogGroup", "ProviderLogGroup", _LOG_GROUP)]
        )
        self.cfn = _paginated_client(
            {"list_stack_resources": [{"StackResourceSummaries": summaries}]}
        )
        self.logs = MagicMock(name="logs")
        self.lambda_client = MagicMock(name="lambda")
        self.lambda_client.get_function_configuration.return_value = {"LoggingConfig": {}}
        self.eks = MagicMock(name="eks")
        self.eks.describe_cluster.return_value = {"cluster": _eks_identity()}
        _route(
            self.ctx,
            {
                "cloudformation": self.cfn,
                "logs": self.logs,
                "lambda": self.lambda_client,
                "eks": self.eks,
            },
        )
        self.live_stack = (
            _live_stack(status=stack_status) if live_stack == "default" else live_stack
        )
        self.observe = MagicMock(name="observe")

    def run(self, observations: Any) -> list[dict[str, Any]]:
        self.observe.side_effect = observations
        with _patched_helpers(
            {
                "describe_stack": MagicMock(return_value=self.live_stack),
                "_observe_log_group_stability": self.observe,
            }
        ):
            return ownership_log_groups._checkpoint_owned_log_groups(self.ctx)

    @property
    def records(self) -> list[dict[str, Any]]:
        return self.ctx.checkpoint.state.get("owned_log_groups", [])

    @property
    def incidents(self) -> list[str]:
        return [
            item["phase"]
            for item in self.ctx.checkpoint.state.get("log_group_checkpoint_incidents", [])
        ]

    def observed_names(self) -> list[str]:
        return [call.args[2] for call in self.observe.call_args_list]


class TestCheckpointOwnedLogGroups:
    @pytest.mark.parametrize(
        ("state_override", "match"),
        [
            ({"target_stack_regions": []}, "target_stack_regions must be an object"),
            ({"log_group_cleanup_token": "short"}, "cleanup token is malformed"),
            ({"owned_log_groups": {}}, "owned_log_groups must be a list"),
        ],
    )
    def test_malformed_checkpoint_fails_closed(self, state_override: Any, match: str) -> None:
        harness = _CheckpointHarness(state=_stack_state(**state_override))
        with pytest.raises(RuntimeError, match=match):
            harness.run([])

    def test_created_at_must_be_a_timestamp(self) -> None:
        harness = _CheckpointHarness()
        harness.ctx.checkpoint.created_at = "yesterday"
        with pytest.raises(RuntimeError, match="created_at is not a valid timestamp"):
            harness.run([])

    def test_missing_cleanup_token_is_minted_once(self) -> None:
        harness = _CheckpointHarness(state=_stack_state(owned_stacks={}))
        harness.ctx.checkpoint.state.pop("log_group_cleanup_token")
        assert harness.run([]) == []
        token = harness.ctx.checkpoint.state["log_group_cleanup_token"]
        assert re.fullmatch(r"[0-9a-f]{32}", token)
        harness.ctx.persist_callback.assert_called_once_with(harness.ctx.checkpoint)
        harness.ctx.session.client.assert_not_called()

    @pytest.mark.parametrize(
        "live_stack",
        [None, _live_stack(status="DELETE_IN_PROGRESS"), _live_stack(run_tag="other-run")],
        ids=["absent", "deleting", "foreign-run"],
    )
    def test_stacks_without_live_run_authority_never_create_records(self, live_stack: Any) -> None:
        harness = _CheckpointHarness(live_stack=live_stack)
        assert harness.run([]) == []
        harness.observe.assert_not_called()
        harness.cfn.get_paginator.assert_not_called()

    def test_tagged_live_group_is_checkpointed_with_three_stable_reads(self) -> None:
        harness = _CheckpointHarness(
            [
                _resource("AWS::Logs::LogGroup", "ProviderLogGroup", _LOG_GROUP),
                _resource("AWS::S3::Bucket", "Bucket", "bucket"),
                _resource("AWS::Logs::LogGroup", "Skipped", "", "CREATE_COMPLETE"),
                _resource("AWS::Logs::LogGroup", "Rolling", "rolling", "DELETE_COMPLETE"),
            ]
        )

        records = harness.run([_present(), _present(), _present()])

        assert len(records) == 1
        record = records[0]
        for key, value in _log_group_record().items():
            assert record[key] == value
        assert record["observed_identity"] == _identity()
        assert set(record["checkpoint_observations"]) == {
            "initial",
            "immediate_pre_tag",
            "post_tag",
        }
        assert record["original_generation_disposition"]["status"] == "checkpointed-present"
        assert record["original_generation_disposition"]["original_identity"] == _identity()
        harness.logs.tag_resource.assert_not_called()
        harness.logs.create_log_group.assert_not_called()
        kwargs = [call.kwargs for call in harness.observe.call_args_list]
        assert kwargs[0] == {
            "expected_identity": None,
            "expected_tags": None,
            "required_present": constants._LOG_GROUP_CHECKPOINT_STABLE_OBSERVATIONS,
            "required_absent": constants._LOG_GROUP_CHECKPOINT_STABLE_OBSERVATIONS,
        }
        assert kwargs[1] == {
            "expected_identity": _identity(),
            "expected_tags": None,
            "required_present": 1,
            "required_absent": 1,
        }
        assert kwargs[2] == {
            "expected_identity": _identity(),
            "expected_tags": _AUTHORITY_TAGS,
            "required_present": constants._LOG_GROUP_CHECKPOINT_STABLE_OBSERVATIONS,
            "required_absent": constants._LOG_GROUP_CHECKPOINT_STABLE_OBSERVATIONS,
        }
        assert harness.records == records

    def test_untagged_group_is_tagged_between_the_fenced_reads(self) -> None:
        harness = _CheckpointHarness()
        untagged = _identity(tags={})

        records = harness.run([_present(untagged), _present(untagged), _present()])

        assert records[0]["observed_identity"] == _identity()
        harness.logs.tag_resource.assert_called_once_with(
            resourceArn=_LOG_GROUP_ARN, tags=_AUTHORITY_TAGS
        )

    def test_lambda_default_group_is_derived_from_the_live_configuration(self) -> None:
        harness = _CheckpointHarness([_resource("AWS::Lambda::Function", "Worker", _FUNCTION)])
        default_name = f"/aws/lambda/{_FUNCTION}"
        identity = _identity(arn=f"arn:aws:logs:{_REGION}:{_ACCOUNT}:log-group:{default_name}")

        records = harness.run([_present(identity), _present(identity), _present(identity)])

        assert records[0]["name"] == default_name
        assert records[0]["source_resource_type"] == "AWS::Lambda::Function"
        harness.lambda_client.get_function_configuration.assert_called_once_with(
            FunctionName=_FUNCTION
        )

    def test_lambda_with_a_custom_log_group_derives_nothing(self) -> None:
        harness = _CheckpointHarness([_resource("AWS::Lambda::Function", "Worker", _FUNCTION)])
        harness.lambda_client.get_function_configuration.return_value = {
            "LoggingConfig": {"LogGroup": "/custom/group"}
        }
        assert harness.run([]) == []
        harness.observe.assert_not_called()

    def test_missing_lambda_is_tolerated_only_under_rollback(self) -> None:
        not_found = _client_error("ResourceNotFoundException", "GetFunctionConfiguration")
        harness = _CheckpointHarness([_resource("AWS::Lambda::Function", "Worker", _FUNCTION)])
        harness.lambda_client.get_function_configuration.side_effect = not_found
        with pytest.raises(ClientError):
            harness.run([])

        harness = _CheckpointHarness(
            [_resource("AWS::Lambda::Function", "Worker", _FUNCTION, "DELETE_COMPLETE")],
            stack_status="ROLLBACK_COMPLETE",
        )
        harness.lambda_client.get_function_configuration.side_effect = not_found
        default_name = f"/aws/lambda/{_FUNCTION}"
        identity = _identity(arn=f"arn:aws:logs:{_REGION}:{_ACCOUNT}:log-group:{default_name}")
        records = harness.run([_present(identity), _present(identity), _present(identity)])
        assert records[0]["name"] == default_name

        harness = _CheckpointHarness(
            [_resource("AWS::Lambda::Function", "Worker", _FUNCTION)],
            stack_status="ROLLBACK_COMPLETE",
        )
        harness.lambda_client.get_function_configuration.side_effect = _client_error(
            "AccessDeniedException"
        )
        with pytest.raises(ClientError):
            harness.run([])

    def test_eks_cluster_groups_carry_the_live_cluster_identity(self) -> None:
        harness = _CheckpointHarness([_resource("AWS::EKS::Cluster", "Cluster", _CLUSTER)])

        def observe(client: Any, region: str, name: str, **kwargs: Any) -> dict[str, Any]:
            return _present(_identity(arn=f"arn:aws:logs:{_REGION}:{_ACCOUNT}:log-group:{name}"))

        records = harness.run(observe)

        assert [record["name"] for record in records] == list(
            ownership_log_groups._derived_log_group_names("AWS::EKS::Cluster", _CLUSTER)
        )
        assert all(record["source_service_identity"] == _eks_identity() for record in records)
        harness.eks.describe_cluster.assert_called_once_with(name=_CLUSTER)

    def test_rolled_back_deleted_cluster_uses_the_exact_tombstone_identity(self) -> None:
        harness = _CheckpointHarness(
            [_resource("AWS::EKS::Cluster", "Cluster", _CLUSTER, "DELETE_COMPLETE")],
            stack_status="ROLLBACK_COMPLETE",
        )
        harness.eks.describe_cluster.side_effect = _client_error("ResourceNotFoundException")

        def observe(client: Any, region: str, name: str, **kwargs: Any) -> dict[str, Any]:
            return _present(_identity(arn=f"arn:aws:logs:{_REGION}:{_ACCOUNT}:log-group:{name}"))

        records = harness.run(observe)
        assert len(records) == 5
        assert all(
            record["source_service_identity"] == _eks_identity("DELETED") for record in records
        )

    def test_previous_record_is_revalidated_against_its_own_generation(self) -> None:
        previous = _log_group_record(observed_identity=_identity())
        harness = _CheckpointHarness(state=_stack_state(owned_log_groups=[previous]))

        records = harness.run([_present()])

        assert records == [harness.records[0]]
        assert len(harness.records) == 1
        history = harness.records[0]["identity_observation_history"]
        assert [item["phase"] for item in history] == ["checkpoint-revalidation"]
        assert harness.observe.call_args.kwargs["expected_identity"] == _identity()
        assert harness.observe.call_args.kwargs["expected_tags"] == _AUTHORITY_TAGS
        harness.logs.tag_resource.assert_not_called()

    @pytest.mark.parametrize(
        ("outcome", "status"),
        [
            (
                {"status": "replacement", "observed_generation": {"creation_time": 1}},
                "replacement-observed-during-checkpoint",
            ),
            (_absent(), "checkpoint-generation-not-stable"),
        ],
    )
    def test_previous_generation_that_is_not_stable_is_dispositioned_and_fails(
        self, outcome: dict[str, Any], status: str
    ) -> None:
        previous = _log_group_record(observed_identity=_identity())
        harness = _CheckpointHarness(state=_stack_state(owned_log_groups=[previous]))
        with pytest.raises(RuntimeError, match="checkpoint generation is not stable"):
            harness.run([outcome])
        disposition = harness.records[0]["original_generation_disposition"]
        assert disposition["status"] == status
        assert disposition["phase"] == "checkpoint-revalidation"
        assert disposition["last_observation_status"] == outcome["status"]

    def test_previous_record_drift_fails_closed(self) -> None:
        drifted = _log_group_record(source_logical_id="Other", observed_identity=_identity())
        harness = _CheckpointHarness(state=_stack_state(owned_log_groups=[drifted]))
        with pytest.raises(RuntimeError, match="ownership changed"):
            harness.run([])

        malformed = _log_group_record(observed_identity="not-a-dict")
        harness = _CheckpointHarness(state=_stack_state(owned_log_groups=[malformed]))
        with pytest.raises(RuntimeError, match="identity is malformed"):
            harness.run([])
        harness.observe.assert_not_called()

    def test_absent_explicit_group_fails_under_a_live_stack_but_not_a_rollback(self) -> None:
        harness = _CheckpointHarness()
        with pytest.raises(RuntimeError, match="absent before teardown"):
            harness.run([_absent()])
        assert harness.incidents == ["checkpoint-explicit-group-absence"]
        harness.logs.create_log_group.assert_not_called()

        harness = _CheckpointHarness(
            [_resource("AWS::Logs::LogGroup", "ProviderLogGroup", _LOG_GROUP, "DELETE_COMPLETE")],
            stack_status="ROLLBACK_COMPLETE",
        )
        assert harness.run([_absent()]) == []
        assert harness.incidents == ["checkpoint-explicit-group-absence"]
        harness.logs.create_log_group.assert_not_called()

    def test_absent_lambda_group_is_created_with_authority_tags(self) -> None:
        harness = _CheckpointHarness([_resource("AWS::Lambda::Function", "Worker", _FUNCTION)])
        default_name = f"/aws/lambda/{_FUNCTION}"
        identity = _identity(arn=f"arn:aws:logs:{_REGION}:{_ACCOUNT}:log-group:{default_name}")

        records = harness.run(
            [_absent(), _present(identity), _present(identity), _present(identity)]
        )

        assert records[0]["name"] == default_name
        harness.logs.create_log_group.assert_called_once_with(
            logGroupName=default_name, tags=_AUTHORITY_TAGS
        )
        assert harness.observe.call_args_list[1].kwargs["expected_tags"] == _AUTHORITY_TAGS
        assert harness.incidents == []

    def test_group_appearing_during_creation_is_never_adopted(self) -> None:
        harness = _CheckpointHarness([_resource("AWS::Lambda::Function", "Worker", _FUNCTION)])
        harness.logs.create_log_group.side_effect = _client_error("ResourceAlreadyExistsException")
        with pytest.raises(RuntimeError, match="refusing to adopt or tag it"):
            harness.run([_absent(), _present()])
        assert harness.incidents == ["checkpoint-create-race"]
        assert harness.observe.call_args_list[1].kwargs["expected_tags"] is None
        assert harness.records == []
        harness.logs.tag_resource.assert_not_called()

        harness = _CheckpointHarness([_resource("AWS::Lambda::Function", "Worker", _FUNCTION)])
        harness.logs.create_log_group.side_effect = _client_error("AccessDeniedException")
        with pytest.raises(ClientError):
            harness.run([_absent()])

    @pytest.mark.parametrize(
        ("observations", "phase", "match"),
        [
            ([{"status": "unsettled"}], "checkpoint-initial-stability", "could not be stably"),
            (
                [_present(_identity(tags={constants._RUN_STACK_TAG: "other-run"}))],
                "checkpoint-authority-tag-conflict",
                "authority tags conflict",
            ),
            (
                [_present(), _absent()],
                "checkpoint-immediate-pre-tag",
                "changed immediately before tagging",
            ),
            (
                [_present(), _present(), {"status": "tag-drift"}],
                "checkpoint-post-tag-stability",
                "changed while checkpointing",
            ),
        ],
    )
    def test_unstable_fresh_generation_is_recorded_as_an_incident_not_adopted(
        self, observations: list[dict[str, Any]], phase: str, match: str
    ) -> None:
        harness = _CheckpointHarness()
        with pytest.raises(RuntimeError, match=match):
            harness.run(observations)
        assert harness.incidents == [phase]
        assert harness.records == []
        incident = harness.ctx.checkpoint.state["log_group_checkpoint_incidents"][0]
        assert incident["candidate"]["name"] == _LOG_GROUP
        if phase == "checkpoint-authority-tag-conflict":
            assert incident["outcome"]["status"] == "tag-drift"
            assert incident["outcome"]["tag_drift"] == {
                constants._RUN_STACK_TAG: {"expected": _RUN_ID, "observed": "other-run"}
            }

    @pytest.mark.parametrize(
        ("observations", "match"),
        [
            ([_present(None)], "omitted identity"),
            ([_present(_identity(_RUN_STARTED_MS - 1))], "predates this validation run"),
            ([_present(), _present(), _present(None)], "omitted its post-tag identity"),
        ],
    )
    def test_incomplete_or_pre_existing_identity_fails_closed(
        self, observations: list[dict[str, Any]], match: str
    ) -> None:
        harness = _CheckpointHarness()
        with pytest.raises(RuntimeError, match=match):
            harness.run(observations)
        assert harness.records == []


# ---------------------------------------------------------------------------
# ownership/ecr.py
# ---------------------------------------------------------------------------

_BASELINE_REPO = "baseline/repository"
_NEW_REPO = f"{_PROJECT}/new"
_NEW_REPO_ARN = f"arn:aws:ecr:{_REGION}:{_ACCOUNT}:repository/{_NEW_REPO}"
_IMAGE_IDENTITY = {
    "digest": "sha256:abc",
    "manifest_media_type": "application/vnd.oci.image.manifest.v1+json",
    "artifact_media_type": "",
    "manifest": {"schemaVersion": 2},
}


def _repository(
    name: str = _NEW_REPO,
    *,
    images: list[dict[str, Any]] | None = None,
    tags: Mapping[str, str] | None = "default",
    created_at: str = "2026-08-11T00:00:00+00:00",
) -> dict[str, Any]:
    return {
        "name": name,
        "arn": f"arn:aws:ecr:{_REGION}:{_ACCOUNT}:repository/{name}",
        "registry_id": _ACCOUNT,
        "created_at": created_at,
        "tags": {constants._RUN_STACK_TAG: _RUN_ID} if tags == "default" else dict(tags or {}),
        "images": list(images or []),
    }


def _image(*tags: str, digest: str = "sha256:abc") -> dict[str, Any]:
    return {**_IMAGE_IDENTITY, "digest": digest, "tags": list(tags)}


def _created_record(repository: Mapping[str, Any] | None = None) -> dict[str, Any]:
    repository = repository if repository is not None else _repository()
    return {
        "region": _REGION,
        "name": repository["name"],
        "arn": repository["arn"],
        "creation_identity": ownership_ecr._ecr_creation_identity(repository),
        "run_tag": _RUN_ID,
        "cleanup_policy": "retain-no-conditional-delete",
    }


def _delta_record(tag: str = "run-tag", repository: str = _BASELINE_REPO) -> dict[str, Any]:
    return {
        "region": _REGION,
        "repository": repository,
        "tag": tag,
        "identity": dict(_IMAGE_IDENTITY),
        "cleanup_policy": "retain-no-conditional-delete",
    }


class TestStripBaselineEcr:
    def test_empty_baseline_identities_and_fully_protected_regions_are_handled(self) -> None:
        stack_id = f"arn:aws:cloudformation:{_REGION}:{_ACCOUNT}:stack/GCOGitHubOIDCStack/p-uuid"
        baseline = {
            "protected_stacks": {
                _REGION: [
                    {
                        "stack_id": stack_id,
                        "physical_resources": [
                            {
                                "resource_type": "AWS::IAM::Role",
                                "physical_id": "protected-role",
                                "logical_id": "Role",
                            }
                        ],
                    }
                ]
            },
            "ecr_repositories": {
                _REGION: [
                    {"name": "", "arn": "arn:aws:ecr:us-east-1:123456789012:repository/nameless"},
                    {"name": "arnless", "arn": ""},
                    {"name": _BASELINE_REPO, "arn": ""},
                ]
            },
        }
        inventory = {
            "cloudformation_stacks": {
                _REGION: [{"stack_id": stack_id}],
                _OTHER_REGION: [{"stack_id": "other"}],
            },
            "regional": {_REGION: {"ecr_repositories": [_BASELINE_REPO, "arnless", _NEW_REPO]}},
            "iam_roles": ["protected-role", f"{_PROJECT}-role"],
            "iam_users": [f"{_PROJECT}-user"],
        }

        residual = ownership_ecr._strip_baseline_ecr(inventory, baseline)

        assert residual["cloudformation_stacks"] == {_OTHER_REGION: [{"stack_id": "other"}]}
        assert residual["regional"] == {_REGION: {"ecr_repositories": [_NEW_REPO]}}
        assert residual["iam_roles"] == [f"{_PROJECT}-role"]
        assert residual["iam_users"] == [f"{_PROJECT}-user"]
        assert inventory["iam_roles"] == ["protected-role", f"{_PROJECT}-role"]

    def test_regional_records_are_stripped_only_on_exact_protected_or_live_authority(
        self,
    ) -> None:
        stack_id = f"arn:aws:cloudformation:{_REGION}:{_ACCOUNT}:stack/GCOGitHubOIDCStack/p-uuid"
        protected_arn = f"arn:aws:sqs:{_REGION}:{_ACCOUNT}:protected-queue"
        baseline = {
            "protected_stacks": {
                _REGION: [
                    {
                        "stack_id": stack_id,
                        "physical_resources": [
                            {
                                "resource_type": "AWS::DynamoDB::Table",
                                "physical_id": "protected-table",
                                "logical_id": "Table",
                            },
                            {
                                "resource_type": "AWS::SQS::Queue",
                                "physical_id": protected_arn,
                                "logical_id": "Queue",
                            },
                        ],
                    }
                ]
            },
            "ecr_repositories": {_OTHER_REGION: [{"name": _BASELINE_REPO, "arn": "arn:x"}]},
        }
        live_vpc = "vpc-0123456789abcdef0"
        stale_vpc = "vpc-0fedcba9876543210"
        pod = "ns/worker-abc/12345678-1234-1234-1234-123456789012"
        tagged = [
            {"arn": protected_arn, "tags": {}},
            {
                "arn": f"arn:aws:sqs:{_REGION}:{_ACCOUNT}:stack-queue",
                "tags": {"aws:cloudformation:stack-id": stack_id},
            },
            {"arn": f"arn:aws:ec2:{_REGION}:{_ACCOUNT}:vpc/{live_vpc}", "tags": {}},
            {"arn": f"arn:aws:ec2:{_REGION}:{_ACCOUNT}:vpc/{stale_vpc}", "tags": {}},
            {"arn": f"arn:aws:ec2:{_REGION}:{_ACCOUNT}:volume/vol-0123456789abcdef0", "tags": {}},
            {"arn": f"arn:aws:eks:{_REGION}:{_ACCOUNT}:pod/{_CLUSTER}/{pod}", "tags": {}},
            {"arn": f"arn:aws:eks:{_REGION}:{_ACCOUNT}:pod/gone-cluster/{pod}", "tags": {}},
        ]
        inventory = {
            "authority_scope": {"partition": "aws", "account": _ACCOUNT},
            "coverage": {
                "complete": True,
                "completed_scanners": ["ec2_instances", "ec2_networking", "eks_clusters"],
                "scanner_regions": {
                    "eks_clusters": [_REGION],
                    "ec2_instances": [_REGION],
                    "ec2_networking": [_REGION],
                },
            },
            "authoritative_ec2_resources": {_REGION: {"vpcs": [live_vpc], "volumes": ["vol-1"]}},
            "authoritative_eks_clusters": {_REGION: [_CLUSTER]},
            "regional": {
                _REGION: {
                    "tagged_resources": tagged,
                    "dynamodb_tables": ["protected-table", f"{_PROJECT}-jobs"],
                    "sqs_queues": [protected_arn],
                },
                _OTHER_REGION: {"ecr_repositories": [_BASELINE_REPO]},
            },
        }

        residual = ownership_ecr._strip_baseline_ecr(inventory, baseline)

        assert residual["regional"] == {
            _REGION: {
                "tagged_resources": [
                    tagged[2],
                    tagged[4],
                    tagged[5],
                ],
                "dynamodb_tables": [f"{_PROJECT}-jobs"],
                "sqs_queues": [],
            }
        }


class TestStripExpectedRetainedEcr:
    def _ctx(
        self,
        *,
        created: list[dict[str, Any]] | None = None,
        deltas: list[dict[str, Any]] | None = None,
        baseline_repositories: list[dict[str, Any]] | None = None,
    ) -> Any:
        ctx = _ctx(
            {
                "created_ecr_repositories": list(created or []),
                "retained_ecr_image_deltas": list(deltas or []),
            }
        )
        ctx.checkpoint.baseline = {
            "protected_stacks": {},
            "ecr_regions": [_REGION],
            "ecr_repositories": {
                _REGION: list(
                    baseline_repositories
                    if baseline_repositories is not None
                    else [_repository(_BASELINE_REPO, tags={})]
                )
            },
        }
        return ctx

    @staticmethod
    def _final(*repositories: dict[str, Any]) -> dict[str, Any]:
        return {"protected_stacks": {}, "ecr_repositories": {_REGION: list(repositories)}}

    def test_absent_created_repository_is_accepted_as_already_absent(self) -> None:
        ctx = self._ctx(created=[_created_record()])
        sanitized, accepted = ownership_ecr._strip_expected_retained_ecr(ctx, self._final())
        assert sanitized["ecr_repositories"] == {_REGION: []}
        assert accepted["repositories"] == [
            {"region": _REGION, "name": _NEW_REPO, "arn": _NEW_REPO_ARN, "already_absent": True}
        ]

    def test_revalidated_created_repository_is_retained_and_removed_from_comparison(self) -> None:
        ctx = self._ctx(created=[_created_record()])
        final = self._final(_repository(_BASELINE_REPO, tags={}), _repository())

        sanitized, accepted = ownership_ecr._strip_expected_retained_ecr(ctx, final)

        assert sanitized["ecr_repositories"] == {_REGION: [_repository(_BASELINE_REPO, tags={})]}
        assert accepted["repositories"] == [
            {
                "region": _REGION,
                "name": _NEW_REPO,
                "arn": _NEW_REPO_ARN,
                "retained": True,
                "inventory": _repository(),
            }
        ]
        assert final["ecr_repositories"][_REGION] == [
            _repository(_BASELINE_REPO, tags={}),
            _repository(),
        ]

    def test_created_repository_authority_must_be_unique_and_outside_the_baseline(self) -> None:
        ctx = self._ctx(created=[_created_record(), _created_record()])
        with pytest.raises(RuntimeError, match="Invalid retained ECR repository authority"):
            ownership_ecr._strip_expected_retained_ecr(ctx, self._final())

        ctx = self._ctx(created=[_created_record(_repository(_BASELINE_REPO))])
        with pytest.raises(RuntimeError, match="Invalid retained ECR repository authority"):
            ownership_ecr._strip_expected_retained_ecr(ctx, self._final())

    def test_created_repository_drift_in_the_final_inventory_fails_closed(self) -> None:
        ctx = self._ctx(created=[_created_record()])
        with pytest.raises(RuntimeError, match="Final ECR inventory duplicated"):
            ownership_ecr._strip_expected_retained_ecr(
                ctx, self._final(_repository(), _repository())
            )
        with pytest.raises(RuntimeError, match="repository identity changed"):
            ownership_ecr._strip_expected_retained_ecr(
                ctx, self._final(_repository(created_at="2026-08-12T00:00:00+00:00"))
            )
        with pytest.raises(RuntimeError, match="repository run tag changed"):
            ownership_ecr._strip_expected_retained_ecr(
                ctx, self._final(_repository(tags={constants._RUN_STACK_TAG: "other-run"}))
            )

    def test_image_delta_authority_must_be_unique_and_baseline_scoped(self) -> None:
        ctx = self._ctx(deltas=[_delta_record(), _delta_record()])
        with pytest.raises(RuntimeError, match="Invalid retained ECR image-delta authority"):
            ownership_ecr._strip_expected_retained_ecr(
                ctx, self._final(_repository(_BASELINE_REPO, tags={}))
            )

        ctx = self._ctx(created=[_created_record()], deltas=[_delta_record(repository=_NEW_REPO)])
        with pytest.raises(RuntimeError, match="Invalid retained ECR image-delta authority"):
            ownership_ecr._strip_expected_retained_ecr(ctx, self._final())

        ctx = self._ctx(deltas=[_delta_record(repository="unknown/repository")])
        with pytest.raises(RuntimeError, match="not absent from the baseline"):
            ownership_ecr._strip_expected_retained_ecr(ctx, self._final())

        ctx = self._ctx(
            deltas=[_delta_record()],
            baseline_repositories=[
                _repository(_BASELINE_REPO, images=[_image("run-tag")], tags={})
            ],
        )
        with pytest.raises(RuntimeError, match="not absent from the baseline"):
            ownership_ecr._strip_expected_retained_ecr(ctx, self._final())

    def test_baseline_repository_must_still_be_present_exactly_once(self) -> None:
        ctx = self._ctx(deltas=[_delta_record()])
        with pytest.raises(RuntimeError, match="Baseline ECR repository changed"):
            ownership_ecr._strip_expected_retained_ecr(ctx, self._final())

    def test_delta_tag_resolution_is_exact(self) -> None:
        ctx = self._ctx(deltas=[_delta_record()])
        duplicated = _repository(
            _BASELINE_REPO,
            images=[_image("run-tag"), _image("run-tag", digest="sha256:def")],
            tags={},
        )
        with pytest.raises(RuntimeError, match="resolves to multiple images"):
            ownership_ecr._strip_expected_retained_ecr(ctx, self._final(duplicated))

        changed = _repository(
            _BASELINE_REPO, images=[_image("run-tag", digest="sha256:def")], tags={}
        )
        with pytest.raises(RuntimeError, match="image identity changed"):
            ownership_ecr._strip_expected_retained_ecr(ctx, self._final(changed))

        sanitized, accepted = ownership_ecr._strip_expected_retained_ecr(
            ctx, self._final(_repository(_BASELINE_REPO, tags={}))
        )
        assert accepted["image_deltas"] == [
            {
                "region": _REGION,
                "repository": _BASELINE_REPO,
                "tag": "run-tag",
                "already_absent": True,
            }
        ]
        assert sanitized["ecr_repositories"][_REGION][0]["images"] == []

    def test_retained_delta_removes_only_the_run_tag(self) -> None:
        ctx = self._ctx(deltas=[_delta_record()])
        shared = _repository(_BASELINE_REPO, images=[_image("run-tag", "latest")], tags={})

        sanitized, accepted = ownership_ecr._strip_expected_retained_ecr(ctx, self._final(shared))

        assert sanitized["ecr_repositories"][_REGION][0]["images"] == [_image("latest")]
        assert accepted["image_deltas"] == [
            {
                "region": _REGION,
                "repository": _BASELINE_REPO,
                "tag": "run-tag",
                "digest": "sha256:abc",
                "retained": True,
            }
        ]

    def test_retag_of_a_baseline_digest_keeps_the_image(self) -> None:
        ctx = self._ctx(
            deltas=[_delta_record()],
            baseline_repositories=[_repository(_BASELINE_REPO, images=[_image("v1")], tags={})],
        )
        final = self._final(_repository(_BASELINE_REPO, images=[_image("run-tag")], tags={}))

        sanitized, accepted = ownership_ecr._strip_expected_retained_ecr(ctx, final)

        assert sanitized["ecr_repositories"][_REGION][0]["images"] == [_image()]
        assert accepted["image_deltas"][0]["retained"] is True

    def test_new_digest_carrying_only_the_run_tag_is_removed_entirely(self) -> None:
        ctx = self._ctx(
            deltas=[_delta_record()],
            baseline_repositories=[
                _repository(_BASELINE_REPO, images=[_image("v1", digest="sha256:v1")], tags={})
            ],
        )
        final = self._final(
            _repository(
                _BASELINE_REPO,
                images=[_image("v1", digest="sha256:v1"), _image("run-tag")],
                tags={},
            )
        )

        sanitized, accepted = ownership_ecr._strip_expected_retained_ecr(ctx, final)

        assert sanitized["ecr_repositories"][_REGION][0]["images"] == [
            _image("v1", digest="sha256:v1")
        ]
        assert accepted["image_deltas"][0]["digest"] == "sha256:abc"

    def test_ambiguous_baseline_digest_fails_closed(self) -> None:
        ctx = self._ctx(
            deltas=[_delta_record()],
            baseline_repositories=[
                _repository(_BASELINE_REPO, images=[_image("v1"), _image("v2")], tags={})
            ],
        )
        final = self._final(_repository(_BASELINE_REPO, images=[_image("run-tag")], tags={}))
        with pytest.raises(RuntimeError, match="digest is ambiguous"):
            ownership_ecr._strip_expected_retained_ecr(ctx, final)


class TestStripAcceptedRetainedEcr:
    def test_only_revalidated_retained_repositories_are_excluded(self) -> None:
        accepted = {
            "repositories": [
                {"region": _REGION, "name": _NEW_REPO, "retained": True},
                {"region": _REGION, "name": "absent/repo", "already_absent": True},
            ]
        }
        inventory = {
            "regional": {
                _REGION: {"ecr_repositories": [_NEW_REPO, "absent/repo"], "sqs_queues": ["q"]},
                _OTHER_REGION: {"ecr_repositories": [_NEW_REPO]},
            }
        }
        residual = ownership_ecr._strip_accepted_retained_ecr(inventory, accepted)
        assert residual["regional"] == {
            _REGION: {"ecr_repositories": ["absent/repo"], "sqs_queues": ["q"]},
            _OTHER_REGION: {"ecr_repositories": [_NEW_REPO]},
        }

        residual = ownership_ecr._strip_accepted_retained_ecr(
            {"regional": {_REGION: {"ecr_repositories": [_NEW_REPO]}}}, accepted
        )
        assert residual == {"regional": {}}


class TestExpectedEcrImages:
    def _ctx(self, tmp_path: Path, *, mirror_enabled: bool = False) -> Any:
        ctx = _ctx({})
        ctx.settings.repo_root = tmp_path
        ctx.deployment_regions = (_REGION, _OTHER_REGION)
        (tmp_path / "cdk.out").mkdir()
        (tmp_path / "cdk.json").write_text(
            json.dumps(
                {
                    "context": {
                        "project_name": _PROJECT,
                        "volcano_image_mirror": {
                            "enabled": mirror_enabled,
                            "ecr_namespace": f"{_PROJECT}/dockerhub",
                        },
                    }
                }
            ),
            encoding="utf-8",
        )
        return ctx

    @staticmethod
    def _write_assets(tmp_path: Path, stack_name: str, document: Any) -> Path:
        path = tmp_path / "cdk.out" / f"{stack_name}.assets.json"
        path.write_text(
            document if isinstance(document, str) else json.dumps(document), encoding="utf-8"
        )
        return path

    @staticmethod
    def _asset(region: str, repository: str, tag: str) -> dict[str, Any]:
        return {
            "destinations": {
                "current_account-current_region": {
                    "region": region,
                    "repositoryName": repository,
                    "imageTag": tag,
                }
            }
        }

    def _expected(self, ctx: Any, stack_names: list[str], refs: list[str] | None = None) -> Any:
        with patch("cli._image_mirror.collect_source_refs", return_value=list(refs or [])):
            return ownership_ecr._expected_ecr_images(ctx, stack_names)

    def test_targets_are_merged_across_stacks_and_sorted(self, tmp_path: Path) -> None:
        ctx = self._ctx(tmp_path)
        duplicated_destination = self._asset(_REGION, "cdk-assets", "tag-b")
        duplicated_destination["destinations"]["mirror"] = dict(
            duplicated_destination["destinations"]["current_account-current_region"]
        )
        self._write_assets(
            tmp_path,
            _STACK,
            {
                "dockerImages": {
                    "asset-b": duplicated_destination,
                    "asset-a": self._asset(_REGION, "cdk-assets", "tag-a"),
                }
            },
        )
        self._write_assets(
            tmp_path,
            _GLOBAL_STACK,
            {"dockerImages": {"asset-a": self._asset(_REGION, "cdk-assets", "tag-a")}},
        )

        targets = self._expected(ctx, [_STACK, _GLOBAL_STACK])

        assert targets == [
            {
                "region": _REGION,
                "repository": "cdk-assets",
                "tag": "tag-a",
                "sources": [
                    {"kind": "cdk-asset", "stack": _GLOBAL_STACK, "asset_id": "asset-a"},
                    {"kind": "cdk-asset", "stack": _STACK, "asset_id": "asset-a"},
                ],
            },
            {
                "region": _REGION,
                "repository": "cdk-assets",
                "tag": "tag-b",
                "sources": [{"kind": "cdk-asset", "stack": _STACK, "asset_id": "asset-b"}],
            },
        ]

    def test_configured_mirror_adds_one_target_per_deployment_region(self, tmp_path: Path) -> None:
        ctx = self._ctx(tmp_path, mirror_enabled=True)
        self._write_assets(tmp_path, _STACK, {"dockerImages": {}})

        targets = self._expected(ctx, [_STACK], refs=["docker.io/volcanosh/vc-scheduler:v1.15.0"])

        assert targets == [
            {
                "region": region,
                "repository": f"{_PROJECT}/dockerhub/volcanosh/vc-scheduler",
                "tag": "v1.15.0",
                "sources": [
                    {
                        "kind": "configured-mirror",
                        "source_ref": "docker.io/volcanosh/vc-scheduler:v1.15.0",
                    }
                ],
            }
            for region in sorted((_REGION, _OTHER_REGION))
        ]

    @pytest.mark.parametrize(
        ("document", "match"),
        [
            (None, "Could not read cloud assembly assets"),
            ("{not json", "Could not read cloud assembly assets"),
            (["not", "an", "object"], "omitted dockerImages"),
            ({"dockerImages": []}, "omitted dockerImages"),
            ({"dockerImages": {"asset": "not-an-object"}}, "has no destinations"),
            ({"dockerImages": {"asset": {"destinations": []}}}, "has no destinations"),
            ({"dockerImages": {"asset": {"destinations": {"d": "x"}}}}, "is malformed"),
            (
                {"dockerImages": {"asset": {"destinations": {"d": {"repositoryName": "r"}}}}},
                "Invalid expected ECR target",
            ),
        ],
    )
    def test_malformed_cloud_assembly_fails_closed(
        self, tmp_path: Path, document: Any, match: str
    ) -> None:
        ctx = self._ctx(tmp_path)
        if document is not None:
            self._write_assets(tmp_path, _STACK, document)
        with pytest.raises(RuntimeError, match=match):
            self._expected(ctx, [_STACK])


class TestRecordEcrRepositoryCreation:
    def _ctx(self, *, expected: bool = True, in_baseline: bool = False) -> Any:
        ctx = _ctx(
            {
                "expected_ecr_images": (
                    [{"region": _REGION, "repository": _NEW_REPO, "tag": "asset"}]
                    if expected
                    else []
                )
            }
        )
        ctx.checkpoint.baseline = {
            "ecr_regions": [_REGION],
            "ecr_repositories": {_REGION: [_repository(_NEW_REPO)] if in_baseline else []},
        }
        return ctx

    @staticmethod
    def _acknowledgement(**overrides: Any) -> dict[str, Any]:
        response: dict[str, Any] = {
            "repositoryName": _NEW_REPO,
            "repositoryArn": _NEW_REPO_ARN,
            "registryId": _ACCOUNT,
            "createdAt": datetime(2026, 8, 11, tzinfo=UTC),
        }
        response.update(overrides)
        return response

    def test_acknowledgement_is_persisted_once_with_retain_policy(self) -> None:
        ctx = self._ctx()
        ownership_ecr._record_ecr_repository_creation(ctx, _REGION, self._acknowledgement())
        ownership_ecr._record_ecr_repository_creation(ctx, _REGION, self._acknowledgement())

        assert ctx.checkpoint.state["created_ecr_repositories"] == [
            {
                "region": _REGION,
                "name": _NEW_REPO,
                "arn": _NEW_REPO_ARN,
                "creation_identity": {
                    "name": _NEW_REPO,
                    "arn": _NEW_REPO_ARN,
                    "registry_id": _ACCOUNT,
                    "created_at": "2026-08-11T00:00:00+00:00",
                },
                "run_tag": _RUN_ID,
                "cleanup_policy": "retain-no-conditional-delete",
            }
        ]
        assert ctx.persist_callback.call_count == 2

    def test_string_created_at_is_kept_verbatim(self) -> None:
        ctx = self._ctx()
        ownership_ecr._record_ecr_repository_creation(
            ctx, _REGION, self._acknowledgement(createdAt="2026-08-11T00:00:00Z")
        )
        record = ctx.checkpoint.state["created_ecr_repositories"][0]
        assert record["creation_identity"]["created_at"] == "2026-08-11T00:00:00Z"

    def test_unexpected_or_baseline_repositories_are_refused(self) -> None:
        with pytest.raises(RuntimeError, match="Unexpected ECR repository creation"):
            ownership_ecr._record_ecr_repository_creation(
                self._ctx(expected=False), _REGION, self._acknowledgement()
            )
        with pytest.raises(RuntimeError, match="Unexpected ECR repository creation"):
            ownership_ecr._record_ecr_repository_creation(
                self._ctx(in_baseline=True), _REGION, self._acknowledgement()
            )

    @pytest.mark.parametrize(
        "overrides", [{"repositoryArn": ""}, {"registryId": None}, {"createdAt": None}]
    )
    def test_incomplete_acknowledgement_is_refused(self, overrides: dict[str, Any]) -> None:
        ctx = self._ctx()
        with pytest.raises(RuntimeError, match="acknowledgement is incomplete"):
            ownership_ecr._record_ecr_repository_creation(
                ctx, _REGION, self._acknowledgement(**overrides)
            )
        assert "created_ecr_repositories" not in ctx.checkpoint.state

    def test_changed_or_duplicated_acknowledgements_fail_closed(self) -> None:
        ctx = self._ctx()
        ownership_ecr._record_ecr_repository_creation(ctx, _REGION, self._acknowledgement())
        with pytest.raises(RuntimeError, match="acknowledgement changed"):
            ownership_ecr._record_ecr_repository_creation(
                ctx, _REGION, self._acknowledgement(registryId="999999999999")
            )

        records = ctx.checkpoint.state["created_ecr_repositories"]
        records.append(dict(records[0]))
        with pytest.raises(RuntimeError, match="Duplicate ECR creation acknowledgements"):
            ownership_ecr._record_ecr_repository_creation(ctx, _REGION, self._acknowledgement())


class TestCheckpointNewEcrRepositories:
    def _run(self, ctx: Any, live: dict[str, list[dict[str, Any]]]) -> list[dict[str, Any]]:
        with patch_live_validation_helper("collect_ecr_inventory", return_value=live) as collect:
            records = ownership_ecr._checkpoint_new_ecr_repositories(ctx)
        collect.assert_called_once_with(ctx.session, {_REGION})
        return records

    def test_no_acknowledgements_means_nothing_to_reconcile(self) -> None:
        ctx = _ctx({})
        with patch_live_validation_helper("collect_ecr_inventory") as collect:
            assert ownership_ecr._checkpoint_new_ecr_repositories(ctx) == []
        collect.assert_not_called()

    def test_live_repositories_are_reconciled_by_creation_identity(self) -> None:
        ctx = _ctx({"created_ecr_repositories": [_created_record()]})
        records = self._run(ctx, {_REGION: [_repository()]})
        assert records[0]["last_observed"] == ownership_ecr._ecr_creation_identity(_repository())
        assert "observed_absent" not in records[0]
        ctx.persist.assert_called_once_with()

        ctx = _ctx({"created_ecr_repositories": [_created_record()]})
        records = self._run(ctx, {_REGION: []})
        assert records[0]["observed_absent"] is True

    def test_identity_or_run_tag_drift_fails_closed(self) -> None:
        ctx = _ctx({"created_ecr_repositories": [_created_record()]})
        with pytest.raises(RuntimeError, match="repository identity changed"):
            self._run(ctx, {_REGION: [_repository(created_at="2026-08-12T00:00:00+00:00")]})
        ctx = _ctx({"created_ecr_repositories": [_created_record()]})
        with pytest.raises(RuntimeError, match="repository run tag changed"):
            self._run(ctx, {_REGION: [_repository(tags={constants._RUN_STACK_TAG: "other-run"})]})


class TestCheckpointNewEcrImages:
    def _ctx(
        self,
        *,
        baseline_images: list[dict[str, Any]] | None = None,
        expected: list[dict[str, Any]] | None = None,
        previous: list[dict[str, Any]] | None = None,
    ) -> Any:
        state: dict[str, Any] = {
            "expected_ecr_images": (
                expected
                if expected is not None
                else [
                    {
                        "region": _REGION,
                        "repository": _BASELINE_REPO,
                        "tag": "run-tag",
                        "sources": [{"kind": "cdk-asset", "stack": _STACK, "asset_id": "a"}],
                    },
                    {"region": _REGION, "repository": _NEW_REPO, "tag": "asset"},
                ]
            )
        }
        if previous is not None:
            state["retained_ecr_image_deltas"] = previous
        ctx = _ctx(state)
        ctx.checkpoint.baseline = {
            "ecr_regions": [_REGION],
            "ecr_repositories": {
                _REGION: [_repository(_BASELINE_REPO, images=baseline_images or [], tags={})]
            },
        }
        return ctx

    def _run(self, ctx: Any, live: dict[str, list[dict[str, Any]]]) -> list[dict[str, Any]]:
        with patch_live_validation_helper("collect_ecr_inventory", return_value=live) as collect:
            deltas = ownership_ecr._checkpoint_new_ecr_images(ctx)
        collect.assert_called_once_with(ctx.session, [_REGION])
        return deltas

    def test_baseline_is_required(self) -> None:
        ctx = self._ctx()
        ctx.checkpoint.baseline = None
        with pytest.raises(RuntimeError, match="without a baseline"):
            ownership_ecr._checkpoint_new_ecr_images(ctx)

    def test_new_tag_in_a_baseline_repository_is_recorded_as_a_retained_delta(self) -> None:
        ctx = self._ctx()
        live = {
            _REGION: [
                _repository(_BASELINE_REPO, images=[_image("run-tag")], tags={}),
                _repository(_NEW_REPO, images=[_image("asset")]),
            ]
        }

        deltas = self._run(ctx, live)

        assert deltas == [
            {
                "region": _REGION,
                "repository": _BASELINE_REPO,
                "tag": "run-tag",
                "identity": _IMAGE_IDENTITY,
                "sources": [{"kind": "cdk-asset", "stack": _STACK, "asset_id": "a"}],
                "cleanup_policy": "retain-no-conditional-delete",
            }
        ]
        assert ctx.checkpoint.state["retained_ecr_image_deltas"] == deltas
        assert ctx.checkpoint.state["owned_ecr_images"] == []

    def test_unchanged_or_unpushed_expected_tags_produce_no_delta(self) -> None:
        ctx = self._ctx(baseline_images=[_image("run-tag")])
        live = {_REGION: [_repository(_BASELINE_REPO, images=[_image("run-tag")], tags={})]}
        assert self._run(ctx, live) == []

        ctx = self._ctx()
        live = {_REGION: [_repository(_BASELINE_REPO, tags={})]}
        assert self._run(ctx, live) == []

    def test_baseline_repository_or_tag_changes_fail_closed(self) -> None:
        ctx = self._ctx()
        with pytest.raises(RuntimeError, match="Baseline ECR repository disappeared"):
            self._run(ctx, {_REGION: []})

        ctx = self._ctx(baseline_images=[_image("run-tag")])
        with pytest.raises(RuntimeError, match="Baseline ECR tag changed"):
            self._run(ctx, {_REGION: [_repository(_BASELINE_REPO, tags={})]})

        ctx = self._ctx(baseline_images=[_image("run-tag")])
        live = {
            _REGION: [
                _repository(
                    _BASELINE_REPO, images=[_image("run-tag", digest="sha256:def")], tags={}
                )
            ]
        }
        with pytest.raises(RuntimeError, match="Baseline ECR tag changed"):
            self._run(ctx, live)

    def test_delta_without_a_digest_fails_closed(self) -> None:
        ctx = self._ctx()
        live = {
            _REGION: [_repository(_BASELINE_REPO, images=[_image("run-tag", digest="")], tags={})]
        }
        with pytest.raises(RuntimeError, match="lacks a digest"):
            self._run(ctx, live)

    def test_deltas_must_not_change_across_observations(self) -> None:
        ctx = self._ctx(previous=[])
        live = {_REGION: [_repository(_BASELINE_REPO, images=[_image("run-tag")], tags={})]}
        with pytest.raises(RuntimeError, match="image deltas changed"):
            self._run(ctx, live)

    def test_tag_lookup_refuses_ambiguous_repositories(self) -> None:
        repository = _repository(
            _BASELINE_REPO, images=[_image("run-tag"), _image("run-tag", digest="sha256:def")]
        )
        with pytest.raises(RuntimeError, match="resolved to multiple digests"):
            ownership_ecr._image_with_tag(repository, "run-tag")
        assert ownership_ecr._image_with_tag(_repository(), "run-tag") is None
