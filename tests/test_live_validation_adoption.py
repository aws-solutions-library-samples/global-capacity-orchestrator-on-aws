"""Run-tag adoption in the shared live-validation ownership layer.

The upgrade harness (``scripts/upgrade_validation``) cannot own its stacks
through change sets it prepared, because ``gco`` deploys them from another
process. ``ownership/stacks.py`` therefore has a second, deliberately narrower
authority for that harness alone: a target stack carrying this run's exact tag
and created after the phase began is adopted, and a workload stack the upgrade
recreates becomes a new generation whose predecessor stays on record, so the
KMS key and log groups the predecessor retained remain attributable to the
run. These tests pin that the relaxation is closed to every other harness, that
each adoption rule fails closed, and that the history reaches the KMS,
log-group, and ECR bookkeeping without changing the release harness's
behavior. Every AWS call is faked.
"""

from __future__ import annotations

import copy
import json
from dataclasses import fields
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from scripts.live_release_validation import constants
from scripts.live_release_validation.models import RunSettings
from scripts.live_release_validation.ownership import ecr as ownership_ecr
from scripts.live_release_validation.ownership import kms as ownership_kms
from scripts.live_release_validation.ownership import log_groups as ownership_log_groups
from scripts.live_release_validation.ownership import stacks as ownership_stacks
from tests.test_live_validation_ownership import (
    _ACCOUNT,
    _AUTHORITY_TAGS,
    _CLUSTER,
    _FUNCTION,
    _GLOBAL_STACK,
    _GLOBAL_STACK_ID,
    _REGION,
    _RUN_ID,
    _RUN_STARTED_MS,
    _STACK,
    _STACK_ID,
    _absent,
    _CheckpointHarness,
    _client_error,
    _ctx,
    _eks_identity,
    _identity,
    _kms_record,
    _live_stack,
    _log_group_record,
    _owned_stack,
    _present,
    _resource,
    _stack_state,
)

_WINDOW = datetime(2026, 9, 29, 12, 0, tzinfo=UTC)
_NEW_STACK_ID = f"arn:aws:cloudformation:{_REGION}:{_ACCOUNT}:stack/{_STACK}/new-uuid"


def _adopted(stack_id: str = _STACK_ID, **overrides: Any) -> dict[str, Any]:
    record: dict[str, Any] = {
        "name": _STACK,
        "region": _REGION,
        "stack_id": stack_id,
        "run_tag": _RUN_ID,
        "authority": ownership_stacks._RUN_TAG_ADOPTION_AUTHORITY,
        "adoption": {
            "phase": "deploy",
            "window_started_at": _WINDOW.isoformat(),
            "creation_time": (_WINDOW + timedelta(minutes=1)).isoformat(),
            "adopted_at": "2026-09-29T12:30:00+00:00",
            "status": "CREATE_COMPLETE",
        },
        "replaced_generations": [],
    }
    record.update(overrides)
    return record


def _adoption_ctx(
    owned: dict[str, dict[str, Any]] | None = None,
    *,
    targets: dict[str, str] | None = None,
    allowed: bool = True,
) -> Any:
    ctx = _ctx(
        _stack_state(
            target_stack_regions=targets if targets is not None else {_STACK: _REGION},
            owned_stacks={_REGION: owned} if owned is not None else {},
        )
    )
    ctx.settings.allows_run_tag_adoption = allowed
    return ctx


def _cloudformation(
    described: dict[str, dict[str, Any] | None],
    *,
    created: dict[str, Any] | None = None,
) -> MagicMock:
    """A CloudFormation client answering describe_stacks by name or ID."""
    client = MagicMock(name="cloudformation")

    def describe_stacks(StackName: str) -> dict[str, Any]:
        if StackName in described:
            stack = described[StackName]
        else:
            stack = next(
                (item for item in described.values() if item and item["stack_id"] == StackName),
                None,
            )
        if stack is None:
            raise _client_error("ValidationError", message=f"Stack {StackName} does not exist")
        response = {
            "StackName": stack["name"],
            "StackId": stack["stack_id"],
            "StackStatus": stack["status"],
            "Tags": [{"Key": key, "Value": value} for key, value in stack["tags"].items()],
        }
        creation = (created or {}).get(StackName, _WINDOW + timedelta(minutes=5))
        if creation is not None:
            response["CreationTime"] = creation
        return {"Stacks": [response]}

    client.describe_stacks.side_effect = describe_stacks
    return client


def _route_cloudformation(ctx: Any, client: MagicMock) -> None:
    ctx.session.client.side_effect = lambda service, **_kwargs: {"cloudformation": client}[service]


# ─── The gate ────────────────────────────────────────────────────────


class TestAdoptionGate:
    def test_only_the_upgrade_settings_class_opts_in(self) -> None:
        from scripts.upgrade_validation.models import UpgradeRunSettings

        assert RunSettings.allows_run_tag_adoption is False
        assert UpgradeRunSettings.allows_run_tag_adoption is True
        # A class attribute, not a field: no constructor argument can turn it on.
        assert "allows_run_tag_adoption" not in {field.name for field in fields(RunSettings)}

    def test_settings_without_the_attribute_never_adopt(self) -> None:
        ctx = _ctx()
        assert ownership_stacks._run_tag_adoption_allowed(ctx) is False
        ctx.settings.allows_run_tag_adoption = "yes"
        assert ownership_stacks._run_tag_adoption_allowed(ctx) is False
        ctx.settings.allows_run_tag_adoption = True
        assert ownership_stacks._run_tag_adoption_allowed(ctx) is True


class TestRequireStackAuthority:
    def test_prepared_records_keep_the_prepared_rules(self) -> None:
        ctx = _adoption_ctx()
        ownership_stacks._require_stack_authority(
            ctx, _owned_stack(), region=_REGION, stack_name=_STACK
        )
        with pytest.raises(RuntimeError, match="prepared-change-set authority"):
            ownership_stacks._require_stack_authority(
                ctx, _owned_stack(change_set_id=""), region=_REGION, stack_name=_STACK
            )

    def test_adopted_records_need_the_opt_in(self) -> None:
        with pytest.raises(RuntimeError, match="only the upgrade validation harness"):
            ownership_stacks._require_stack_authority(
                _adoption_ctx(allowed=False), _adopted(), region=_REGION, stack_name=_STACK
            )

    @pytest.mark.parametrize(
        "adoption",
        [
            None,
            "deploy",
            {"window_started_at": "x", "creation_time": "y"},
            {"phase": "deploy", "creation_time": "y"},
            {"phase": "deploy", "window_started_at": "x"},
        ],
    )
    def test_adopted_records_need_their_evidence(self, adoption: Any) -> None:
        with pytest.raises(RuntimeError, match="lacks persisted run-tag adoption evidence"):
            ownership_stacks._require_stack_authority(
                _adoption_ctx(), _adopted(adoption=adoption), region=_REGION, stack_name=_STACK
            )

    def test_complete_adoption_is_accepted(self) -> None:
        ownership_stacks._require_stack_authority(
            _adoption_ctx(), _adopted(), region=_REGION, stack_name=_STACK
        )


class TestOwnedStackIds:
    def test_an_unowned_name_has_no_ids(self) -> None:
        assert ownership_stacks._owned_stack_ids(_adoption_ctx({}), _REGION, _STACK) == frozenset()

    def test_a_prepared_stack_owns_one_id(self) -> None:
        ctx = _ctx()
        assert ownership_stacks._owned_stack_ids(ctx, _REGION, _STACK) == {_STACK_ID}

    def test_an_adopted_stack_owns_its_replaced_generations(self) -> None:
        record = _adopted(_NEW_STACK_ID, replaced_generations=[{"stack_id": _STACK_ID}])
        ctx = _adoption_ctx({_STACK: record})
        assert ownership_stacks._owned_stack_ids(ctx, _REGION, _STACK) == {
            _STACK_ID,
            _NEW_STACK_ID,
        }

    @pytest.mark.parametrize(
        ("record", "match"),
        [
            (_adopted(replaced_generations={"stack_id": "x"}), "malformed"),
            (_owned_stack(replaced_generations=[{"stack_id": "x"}]), "malformed"),
            (_adopted(replaced_generations=["x"]), "malformed"),
            (_adopted(replaced_generations=[{"stack_id": ""}]), "malformed"),
        ],
    )
    def test_malformed_history_fails_closed(self, record: dict[str, Any], match: str) -> None:
        with pytest.raises(RuntimeError, match=match):
            ownership_stacks._owned_stack_ids(_adoption_ctx({_STACK: record}), _REGION, _STACK)

    def test_history_is_refused_without_the_opt_in(self) -> None:
        record = _adopted(_NEW_STACK_ID, replaced_generations=[{"stack_id": _STACK_ID}])
        with pytest.raises(RuntimeError, match="records replaced generations"):
            ownership_stacks._owned_stack_ids(
                _adoption_ctx({_STACK: record}, allowed=False), _REGION, _STACK
            )


# ─── Adopted records through the existing primitives ────────────────


class TestAdoptedRecordsThroughTheExistingPrimitives:
    def test_prepared_change_set_history_is_empty_for_an_adopted_stack(self) -> None:
        ctx = _adoption_ctx({_STACK: _adopted()})
        assert ownership_stacks._prepared_change_set_authority(ctx) == {_STACK: {}}

    def test_a_change_set_prepared_on_an_adopted_stack_keeps_the_adoption(self) -> None:
        ctx = _adoption_ctx({_STACK: _adopted()})
        change_set = f"arn:aws:cloudformation:{_REGION}:{_ACCOUNT}:changeSet/teardown/cs"
        ownership_stacks._record_prepared_stack_identity(
            ctx, _STACK, _REGION, _STACK_ID, change_set, "UPDATE"
        )
        record = ctx.checkpoint.state["owned_stacks"][_REGION][_STACK]
        assert record["authority"] == ownership_stacks._RUN_TAG_ADOPTION_AUTHORITY
        assert record["adoption"] == _adopted()["adoption"]
        assert set(record["prepared_change_sets"]) == {change_set}
        assert ownership_stacks._prepared_change_set_authority(ctx) == {
            _STACK: {
                change_set: {
                    "change_set_id": change_set,
                    "stack_id": _STACK_ID,
                    "change_set_type": "UPDATE",
                }
            }
        }

    def test_an_adopted_stack_is_observed_and_authorized_by_exact_identity(self) -> None:
        ctx = _adoption_ctx({_STACK: _adopted()})
        assert (
            ownership_stacks._record_stack_identity(ctx, _STACK, _REGION, _live_stack())[
                "authority"
            ]
            == ownership_stacks._RUN_TAG_ADOPTION_AUTHORITY
        )
        _route_cloudformation(ctx, _cloudformation({_STACK_ID: _live_stack()}))
        ownership_stacks._authorize_owned_stack(ctx, _STACK, _REGION, _STACK_ID)
        with pytest.raises(RuntimeError, match="refusing name-based adoption"):
            ownership_stacks._record_stack_identity(
                ctx, _STACK, _REGION, _live_stack(stack_id=_NEW_STACK_ID)
            )


# ─── The adoption primitive ──────────────────────────────────────────


class TestStackCreationTime:
    def test_the_exact_stack_reports_its_creation_time(self) -> None:
        ctx = _adoption_ctx()
        created = datetime(2026, 9, 29, 8, 0, tzinfo=UTC)
        _route_cloudformation(
            ctx, _cloudformation({_STACK_ID: _live_stack()}, created={_STACK_ID: created})
        )
        assert ownership_stacks._stack_creation_time(ctx, _REGION, _STACK_ID) == created

    @pytest.mark.parametrize(
        "response",
        [
            {"Stacks": []},
            {"Stacks": [{"CreationTime": _WINDOW}, {"CreationTime": _WINDOW}]},
            {"Stacks": [{}]},
            {"Stacks": [{"CreationTime": "2026-09-29T12:00:00Z"}]},
            {"Stacks": [{"CreationTime": _WINDOW.replace(tzinfo=None)}]},
        ],
    )
    def test_a_missing_or_naive_creation_time_fails_closed(self, response: Any) -> None:
        ctx = _adoption_ctx()
        client = MagicMock()
        client.describe_stacks.return_value = response
        _route_cloudformation(ctx, client)
        with pytest.raises(RuntimeError, match="omitted the creation time"):
            ownership_stacks._stack_creation_time(ctx, _REGION, _STACK_ID)


class TestAdoptRunTaggedStacks:
    def _adopt(self, ctx: Any, **kwargs: Any) -> dict[str, Any]:
        return ownership_stacks._adopt_run_tagged_stacks(
            ctx, phase=kwargs.pop("phase", "deploy"), window_started_at=_WINDOW, **kwargs
        )

    def test_the_relaxation_is_closed_to_other_harnesses(self) -> None:
        with pytest.raises(RuntimeError, match="not enabled for this harness"):
            self._adopt(_adoption_ctx(allowed=False))

    def test_the_window_must_be_timezone_aware(self) -> None:
        with pytest.raises(ValueError, match="timezone-aware"):
            ownership_stacks._adopt_run_tagged_stacks(
                _adoption_ctx(), phase="deploy", window_started_at=_WINDOW.replace(tzinfo=None)
            )

    @pytest.mark.parametrize("targets", [{}, None])
    def test_targets_are_required(self, targets: Any) -> None:
        ctx = _adoption_ctx()
        ctx.checkpoint.state["target_stack_regions"] = targets
        with pytest.raises(RuntimeError, match="lacks target stack Regions"):
            self._adopt(ctx)

    def test_replaceable_names_must_be_targets(self) -> None:
        with pytest.raises(RuntimeError, match="Replaceable stacks are not targets: other"):
            self._adopt(_adoption_ctx(), replaceable=("other",))

    def test_a_new_tagged_stack_created_in_the_window_is_adopted(self) -> None:
        ctx = _adoption_ctx({})
        created = _WINDOW + timedelta(minutes=3)
        _route_cloudformation(
            ctx, _cloudformation({_STACK: _live_stack()}, created={_STACK_ID: created})
        )
        evidence = self._adopt(ctx)
        assert evidence["adopted"] == [
            {
                "name": _STACK,
                "region": _REGION,
                "stack_id": _STACK_ID,
                "created_at": created.isoformat(),
            }
        ]
        record = ctx.checkpoint.state["owned_stacks"][_REGION][_STACK]
        assert record["authority"] == ownership_stacks._RUN_TAG_ADOPTION_AUTHORITY
        assert record["run_tag"] == _RUN_ID
        assert record["adoption"]["phase"] == "deploy"
        assert record["adoption"]["window_started_at"] == _WINDOW.isoformat()
        assert record["adoption"]["creation_time"] == created.isoformat()
        assert record["replaced_generations"] == []
        ctx.persist_callback.assert_called()

    def test_the_server_clocks_one_second_resolution_is_allowed(self) -> None:
        ctx = _adoption_ctx({})
        created = _WINDOW - timedelta(milliseconds=500)
        _route_cloudformation(
            ctx, _cloudformation({_STACK: _live_stack()}, created={_STACK_ID: created})
        )
        assert self._adopt(ctx)["adopted"][0]["created_at"] == created.isoformat()

    def test_a_stack_created_before_the_phase_is_refused(self) -> None:
        ctx = _adoption_ctx({})
        _route_cloudformation(
            ctx,
            _cloudformation(
                {_STACK: _live_stack()}, created={_STACK_ID: _WINDOW - timedelta(seconds=2)}
            ),
        )
        with pytest.raises(RuntimeError, match="before deploy started"):
            self._adopt(ctx)
        assert ctx.checkpoint.state["owned_stacks"] == {_REGION: {}}

    def test_a_stack_without_this_runs_tag_is_refused(self) -> None:
        ctx = _adoption_ctx({})
        _route_cloudformation(ctx, _cloudformation({_STACK: _live_stack(run_tag="other-run")}))
        with pytest.raises(RuntimeError, match="is not tagged for run 'run-123'"):
            self._adopt(ctx)

    @pytest.mark.parametrize(
        "live",
        [
            _live_stack(name="other"),
            _live_stack(stack_id="arn:aws:cloudformation:us-east-1:999999999999:stack/x/y"),
        ],
    )
    def test_an_invalid_identity_is_refused(self, live: dict[str, Any]) -> None:
        ctx = _adoption_ctx({})
        _route_cloudformation(ctx, _cloudformation({_STACK: live}))
        with pytest.raises(RuntimeError, match="invalid identity"):
            self._adopt(ctx)

    def test_an_unresolvable_partition_fails_closed(self) -> None:
        ctx = _adoption_ctx({})
        ctx.session.get_partition_for_region.return_value = None
        _route_cloudformation(ctx, _cloudformation({_STACK: _live_stack()}))
        with pytest.raises(RuntimeError, match="Could not resolve AWS partition"):
            self._adopt(ctx)

    def test_absent_and_deleted_targets_are_reported_not_adopted(self) -> None:
        ctx = _adoption_ctx({_STACK: _adopted()}, targets={_STACK: _REGION, _GLOBAL_STACK: _REGION})
        _route_cloudformation(
            ctx,
            _cloudformation(
                {
                    _STACK: None,
                    _GLOBAL_STACK: _live_stack(_GLOBAL_STACK, _GLOBAL_STACK_ID, "DELETE_COMPLETE"),
                }
            ),
        )
        evidence = self._adopt(ctx)
        assert evidence["absent"] == [
            {"name": _GLOBAL_STACK, "region": _REGION, "owned_stack_id": None},
            {"name": _STACK, "region": _REGION, "owned_stack_id": _STACK_ID},
        ]
        assert evidence["adopted"] == evidence["replaced"] == evidence["unchanged"] == []

    def test_an_owned_stack_under_the_same_id_is_left_alone(self) -> None:
        record = _adopted()
        ctx = _adoption_ctx({_STACK: copy.deepcopy(record)})
        _route_cloudformation(ctx, _cloudformation({_STACK: _live_stack(status="UPDATE_COMPLETE")}))
        evidence = self._adopt(ctx, phase="upgrade")
        assert evidence["unchanged"] == [
            {"name": _STACK, "region": _REGION, "stack_id": _STACK_ID, "status": "UPDATE_COMPLETE"}
        ]
        assert ctx.checkpoint.state["owned_stacks"][_REGION][_STACK] == record

    def test_a_replacement_needs_the_name_to_be_replaceable(self) -> None:
        ctx = _adoption_ctx({_STACK: _adopted()})
        _route_cloudformation(ctx, _cloudformation({_STACK: _live_stack(stack_id=_NEW_STACK_ID)}))
        with pytest.raises(RuntimeError, match="during upgrade, which may not replace it"):
            self._adopt(ctx, phase="upgrade")

    def test_a_replacement_keeps_the_deleted_generation(self) -> None:
        previous = _adopted()
        ctx = _adoption_ctx({_STACK: copy.deepcopy(previous)})
        _route_cloudformation(
            ctx,
            _cloudformation(
                {
                    _STACK: _live_stack(stack_id=_NEW_STACK_ID),
                    _STACK_ID: _live_stack(status="DELETE_COMPLETE"),
                }
            ),
        )
        evidence = self._adopt(ctx, phase="upgrade", replaceable=(_STACK,))
        assert [item["stack_id"] for item in evidence["replaced"]] == [_NEW_STACK_ID]
        record = ctx.checkpoint.state["owned_stacks"][_REGION][_STACK]
        assert record["stack_id"] == _NEW_STACK_ID
        assert record["adoption"]["phase"] == "upgrade"
        assert record["replaced_generations"] == [
            {
                "stack_id": _STACK_ID,
                "authority": ownership_stacks._RUN_TAG_ADOPTION_AUTHORITY,
                "adoption": previous["adoption"],
                "replaced_by": _NEW_STACK_ID,
                "replaced_during": "upgrade",
                "observed_status": "DELETE_COMPLETE",
            }
        ]
        assert ownership_stacks._owned_stack_ids(ctx, _REGION, _STACK) == {
            _STACK_ID,
            _NEW_STACK_ID,
        }

    def test_a_vanished_previous_generation_is_recorded_as_absent(self) -> None:
        ctx = _adoption_ctx({_STACK: _adopted()})
        _route_cloudformation(
            ctx, _cloudformation({_STACK: _live_stack(stack_id=_NEW_STACK_ID), _STACK_ID: None})
        )
        self._adopt(ctx, phase="upgrade", replaceable=(_STACK,))
        record = ctx.checkpoint.state["owned_stacks"][_REGION][_STACK]
        assert record["replaced_generations"][0]["observed_status"] == "absent"

    def test_two_live_generations_are_refused(self) -> None:
        ctx = _adoption_ctx({_STACK: _adopted()})
        _route_cloudformation(
            ctx,
            _cloudformation(
                {_STACK: _live_stack(stack_id=_NEW_STACK_ID), _STACK_ID: _live_stack()}
            ),
        )
        with pytest.raises(RuntimeError, match="two live generations"):
            self._adopt(ctx, phase="upgrade", replaceable=(_STACK,))


# ─── The history reaches the retained-resource bookkeeping ──────────


def _replaced_generation_ctx() -> Any:
    record = _adopted(_NEW_STACK_ID, replaced_generations=[{"stack_id": _STACK_ID}])
    return _adoption_ctx({_STACK: record})


class TestHistoryInRetainedResources:
    def test_a_replaced_generations_kms_key_stays_owned(self) -> None:
        ctx = _replaced_generation_ctx()
        assert ownership_kms._validated_owned_kms_identity(ctx, _kms_record())[3] == (
            "harness-schedule"
        )
        assert ownership_kms._validated_owned_kms_identity(ctx, _kms_record(stack_id=_NEW_STACK_ID))
        with pytest.raises(RuntimeError, match="stack identity is invalid"):
            ownership_kms._validated_owned_kms_identity(
                ctx, _kms_record(stack_id=f"{_STACK_ID}-other")
            )

    def test_a_replaced_generations_log_group_stays_owned(self) -> None:
        ctx = _replaced_generation_ctx()
        validate = ownership_log_groups._validated_owned_log_group_identity
        assert validate(ctx, _log_group_record())[0] == _REGION
        assert validate(ctx, _log_group_record(stack_id=_NEW_STACK_ID))[0] == _REGION
        with pytest.raises(RuntimeError, match="authority is invalid"):
            validate(ctx, _log_group_record(stack_id=f"{_STACK_ID}-other"))

    def test_the_release_harness_never_accepts_a_history(self) -> None:
        ctx = _replaced_generation_ctx()
        ctx.settings.allows_run_tag_adoption = False
        with pytest.raises(RuntimeError, match="only the upgrade validation harness"):
            ownership_kms._validated_owned_kms_identity(ctx, _kms_record())


# ─── Log groups a recreated stack derives again ─────────────────────


def _log_group_arn(name: str) -> str:
    return f"arn:aws:logs:{_REGION}:{_ACCOUNT}:log-group:{name}"


def _generation_harness(
    resources: list[dict[str, Any]] | None = None,
    *,
    owned_log_groups: list[dict[str, Any]] | None = None,
    **state: Any,
) -> _CheckpointHarness:
    """The upgrade's new stack generation, with the replaced one's records on file."""
    harness = _CheckpointHarness(
        resources,
        live_stack=_live_stack(stack_id=_NEW_STACK_ID),
        state=_stack_state(
            owned_stacks={
                _REGION: {
                    _STACK: _adopted(_NEW_STACK_ID, replaced_generations=[{"stack_id": _STACK_ID}])
                }
            },
            owned_log_groups=(
                owned_log_groups
                if owned_log_groups is not None
                else [_log_group_record(observed_identity=_identity())]
            ),
            **state,
        ),
    )
    harness.ctx.settings.allows_run_tag_adoption = True
    return harness


def _eks_records() -> list[dict[str, Any]]:
    """The base cluster's five log groups, checkpointed under the replaced stack."""
    return [
        _log_group_record(
            name=name,
            source_resource_type="AWS::EKS::Cluster",
            source_logical_id="Cluster",
            source_physical_id=_CLUSTER,
            source_service_identity=_eks_identity(),
            observed_identity=_identity(arn=_log_group_arn(name)),
        )
        for name in ownership_log_groups._derived_log_group_names("AWS::EKS::Cluster", _CLUSTER)
    ]


class TestLogGroupsAcrossStackGenerations:
    """A recreated stack derives its fixed-name log groups again.

    The EKS cluster keeps its name across ``gco upgrade``'s stack cycle, and so
    does every Lambda function with an explicit name. Run
    pr429-upgrade-513adf93 stopped here: the new generation derived the same
    names the replaced one had checkpointed, and the checkpoint refused them
    as an ownership change, which also blocked the guaranteed teardown.
    """

    @pytest.mark.parametrize("previous_logical_id", ["ProviderLogGroup", "RenamedConstruct"])
    def test_a_group_that_outlived_its_stack_is_rebound_to_the_new_generation(
        self, previous_logical_id: str
    ) -> None:
        harness = _generation_harness(
            owned_log_groups=[
                _log_group_record(
                    source_logical_id=previous_logical_id, observed_identity=_identity()
                )
            ]
        )

        records = harness.run([_present()])

        assert harness.records == records
        [record] = records
        assert record["stack_id"] == _NEW_STACK_ID
        assert record["source_logical_id"] == "ProviderLogGroup"
        assert record["observed_identity"] == _identity()
        [binding] = record["stack_generations"]
        assert binding["stack_id"] == _STACK_ID
        assert binding["source_logical_id"] == previous_logical_id
        assert binding["rebound_to"] == _NEW_STACK_ID and binding["rebound_at"]
        assert "source_service_identity" not in binding
        assert [item["phase"] for item in record["identity_observation_history"]] == [
            "checkpoint-stack-generation"
        ]
        assert harness.observe.call_args.kwargs["expected_identity"] == _identity()
        assert harness.observe.call_args.kwargs["expected_tags"] == _AUTHORITY_TAGS
        harness.logs.tag_resource.assert_not_called()
        assert "superseded_log_groups" not in harness.ctx.checkpoint.state

    def test_a_surviving_eks_group_keeps_the_cluster_identity_across_the_rebinding(self) -> None:
        harness = _generation_harness(
            [_resource("AWS::EKS::Cluster", "Cluster", _CLUSTER)], owned_log_groups=_eks_records()
        )

        def observe(client: Any, region: str, name: str, **kwargs: Any) -> dict[str, Any]:
            return _present(kwargs["expected_identity"])

        records = harness.run(observe)

        assert len(records) == 5
        for record in records:
            assert record["stack_id"] == _NEW_STACK_ID
            assert record["source_service_identity"] == _eks_identity()
            assert record["stack_generations"][0]["source_service_identity"] == _eks_identity()

    def test_the_upgrades_deleted_eks_groups_are_superseded_and_checkpointed_afresh(self) -> None:
        """gco's teardown deleted the base cluster's groups; the new cluster made its own."""
        harness = _generation_harness(
            [_resource("AWS::EKS::Cluster", "Cluster", _CLUSTER)], owned_log_groups=_eks_records()
        )
        recreated_at = _RUN_STARTED_MS + 9_000

        def observe(
            client: Any,
            region: str,
            name: str,
            *,
            expected_identity: Any = None,
            expected_tags: Any = None,
            **kwargs: Any,
        ) -> dict[str, Any]:
            fresh = _identity(recreated_at, tags=expected_tags or {}, arn=_log_group_arn(name))
            if expected_identity is not None and expected_identity["creation_time"] != recreated_at:
                return {"status": "replacement", "identity": _identity(recreated_at, tags={})}
            return _present(fresh)

        records = harness.run(observe)

        names = ownership_log_groups._derived_log_group_names("AWS::EKS::Cluster", _CLUSTER)
        assert [record["name"] for record in records] == list(names)
        for record in records:
            assert record["stack_id"] == _NEW_STACK_ID
            assert record["observed_identity"]["creation_time"] == recreated_at
            assert record["observed_identity"]["tags"] == _AUTHORITY_TAGS
            assert "stack_generations" not in record
        superseded = harness.ctx.checkpoint.state["superseded_log_groups"]
        assert [item["name"] for item in superseded] == list(names)
        for item in superseded:
            assert item["stack_id"] == _STACK_ID
            assert item["superseded_by_stack_id"] == _NEW_STACK_ID
            disposition = item["original_generation_disposition"]
            assert disposition["status"] == "superseded-by-stack-generation"
            assert disposition["last_observation_status"] == "replacement"
        assert harness.logs.tag_resource.call_count == 5

    def test_an_absent_lambda_group_is_superseded_and_created_under_run_authority(self) -> None:
        name = f"/aws/lambda/{_FUNCTION}"
        harness = _generation_harness(
            [_resource("AWS::Lambda::Function", "Worker", _FUNCTION)],
            owned_log_groups=[
                _log_group_record(
                    name=name,
                    source_resource_type="AWS::Lambda::Function",
                    source_logical_id="Worker",
                    source_physical_id=_FUNCTION,
                    observed_identity=_identity(arn=_log_group_arn(name)),
                )
            ],
        )
        created = _identity(_RUN_STARTED_MS + 9_000, arn=_log_group_arn(name))

        records = harness.run(
            [_absent(), _absent(), _present(created), _present(created), _present(created)]
        )

        [record] = records
        assert record["stack_id"] == _NEW_STACK_ID
        assert record["observed_identity"] == created
        harness.logs.create_log_group.assert_called_once_with(
            logGroupName=name, tags=_AUTHORITY_TAGS
        )
        [superseded] = harness.ctx.checkpoint.state["superseded_log_groups"]
        assert superseded["original_generation_disposition"]["last_observation_status"] == (
            "absent"
        )

    @pytest.mark.parametrize("status", ["unsettled", "tag-drift"])
    def test_a_carried_generation_that_is_not_stable_fails_closed(self, status: str) -> None:
        harness = _generation_harness()
        with pytest.raises(
            RuntimeError, match=f"checkpoint generation is not stable for .*{status}"
        ):
            harness.run([{"status": status}])
        [record] = harness.records
        assert record["stack_id"] == _STACK_ID
        disposition = record["original_generation_disposition"]
        assert disposition["status"] == "checkpoint-generation-not-stable"
        assert disposition["phase"] == "checkpoint-stack-generation"
        assert "superseded_log_groups" not in harness.ctx.checkpoint.state
        harness.logs.tag_resource.assert_not_called()

    @pytest.mark.parametrize(
        "previous",
        [
            _log_group_record(stack_id=f"{_STACK_ID}-other", observed_identity=_identity()),
            _log_group_record(authority_phase="post-destroy", observed_identity=_identity()),
        ],
        ids=["unowned-stack", "changed-authority"],
    )
    def test_only_a_replaced_generation_hands_its_groups_over(
        self, previous: dict[str, Any]
    ) -> None:
        harness = _generation_harness(owned_log_groups=[previous])
        with pytest.raises(RuntimeError, match="Log-group ownership changed"):
            harness.run([])
        harness.observe.assert_not_called()

    def test_malformed_generation_evidence_fails_closed(self) -> None:
        harness = _generation_harness(
            owned_log_groups=[_log_group_record(observed_identity="not-a-dict")]
        )
        with pytest.raises(RuntimeError, match="identity is malformed"):
            harness.run([])
        harness.observe.assert_not_called()

        harness = _generation_harness(
            owned_log_groups=[
                _log_group_record(observed_identity=_identity(), stack_generations={})
            ]
        )
        with pytest.raises(RuntimeError, match="stack_generations must be a list"):
            harness.run([_present()])

        harness = _generation_harness(superseded_log_groups={})
        with pytest.raises(RuntimeError, match="superseded_log_groups must be a list"):
            harness.run([{"status": "replacement"}])
        assert len(harness.records) == 1

    def test_the_release_harness_still_refuses_any_change(self) -> None:
        harness = _generation_harness()
        harness.ctx.settings.allows_run_tag_adoption = False
        with pytest.raises(RuntimeError, match="only the upgrade validation harness"):
            harness.run([])
        harness.observe.assert_not_called()


class TestPriorReleaseImageTargets:
    _TARGET = {
        "region": _REGION,
        "repository": "cdk-assets",
        "tag": "abc",
        "sources": [{"kind": "cdk-asset", "stack": _STACK, "asset_id": "abc"}],
    }

    def test_without_a_prior_release_the_checkpoint_list_is_used_as_is(self) -> None:
        ctx = _ctx(_stack_state(expected_ecr_images=[self._TARGET]))
        assert ownership_ecr._checkpointed_ecr_image_targets(ctx) == [self._TARGET]
        assert ownership_ecr._checkpointed_ecr_image_targets(_ctx(_stack_state())) == []

    def test_a_prior_release_adds_its_targets_and_merges_shared_ones(self) -> None:
        prior_source = {"kind": "cdk-asset", "stack": _STACK, "asset_id": "old"}
        ctx = _ctx(
            _stack_state(
                expected_ecr_images=[self._TARGET],
                prior_release_ecr_images=[
                    {**self._TARGET, "sources": [prior_source]},
                    {"region": _REGION, "repository": "cdk-assets", "tag": "old"},
                ],
            )
        )
        targets = ownership_ecr._checkpointed_ecr_image_targets(ctx)
        assert [(item["repository"], item["tag"]) for item in targets] == [
            ("cdk-assets", "abc"),
            ("cdk-assets", "old"),
        ]
        assert targets[0]["sources"] == sorted(
            [*self._TARGET["sources"], prior_source],
            key=lambda item: json.dumps(item, sort_keys=True),
        )
        assert targets[1]["sources"] == [{"kind": "unrecorded"}]

    def test_the_merged_targets_feed_repository_and_image_bookkeeping(self) -> None:
        ctx = _ctx(
            _stack_state(
                expected_ecr_images=[],
                prior_release_ecr_images=[self._TARGET],
            )
        )
        ownership_ecr._record_ecr_repository_creation(
            ctx,
            _REGION,
            {
                "repositoryName": "cdk-assets",
                "repositoryArn": f"arn:aws:ecr:{_REGION}:{_ACCOUNT}:repository/cdk-assets",
                "registryId": _ACCOUNT,
                "createdAt": _WINDOW,
            },
        )
        assert [item["name"] for item in ctx.checkpoint.state["created_ecr_repositories"]] == [
            "cdk-assets"
        ]

    def test_the_expected_images_are_read_from_the_given_checkout(self, tmp_path: Path) -> None:
        (tmp_path / "cdk.out").mkdir()
        (tmp_path / "cdk.json").write_text(json.dumps({"context": {}}), encoding="utf-8")
        (tmp_path / "cdk.out" / f"{_STACK}.assets.json").write_text(
            json.dumps(
                {
                    "dockerImages": {
                        "img": {
                            "destinations": {
                                "d": {
                                    "region": _REGION,
                                    "repositoryName": "cdk-assets",
                                    "imageTag": "base-tag",
                                }
                            }
                        }
                    }
                }
            ),
            encoding="utf-8",
        )
        ctx = _ctx()
        ctx.settings.repo_root = tmp_path / "elsewhere"
        assert ownership_ecr._expected_ecr_images(ctx, [_STACK], root=tmp_path) == [
            {
                "region": _REGION,
                "repository": "cdk-assets",
                "tag": "base-tag",
                "sources": [{"kind": "cdk-asset", "stack": _STACK, "asset_id": "img"}],
            }
        ]


def test_the_run_tag_key_is_the_one_the_harness_writes() -> None:
    assert constants._RUN_STACK_TAG == "GcoLiveValidationRun"
