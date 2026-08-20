"""Offline contracts for the strict pre-destroy PVC/PV/EBS inventory action.

Covers live-object record building (bound EBS PVCs, unbound/non-EBS/missing
handles), the normalized EBS observations the action records, the separate
Prometheus and Alertmanager size assertions, and the action itself: the disabled
no-op, the authorization gate that precedes every EKS/EC2 request, atomic
checkpoint persistence, per-PVC continuation, and the registry/export/docs
lockstep. Every AWS and Kubernetes boundary is mocked.
"""

from __future__ import annotations

import dataclasses
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from botocore.exceptions import ClientError

from cli.volume_cleanup import RegionalVolumeTarget
from scripts.live_release_validation.actions import volume_inventory
from scripts.live_release_validation.checks import volumes as checks_volumes
from scripts.live_release_validation.models import (
    RunCheckpoint,
    RunContext,
    RunSettings,
    ValidationReport,
)
from scripts.live_release_validation.ownership import volumes as ownership_volumes
from scripts.live_release_validation.registry import build_action_registry

_ACCOUNT = "123456789012"
_CALLER_ARN = f"arn:aws:iam::{_ACCOUNT}:role/live-validation"
_BRANCH = "chore/volumes"
_PROJECT = "gco-live"
_REGION = "us-east-1"
_STACK_NAME = f"{_PROJECT}-{_REGION}"
_STACK_ID = f"arn:aws:cloudformation:{_REGION}:{_ACCOUNT}:stack/{_STACK_NAME}/abc"
_CLUSTER_TAG_KEY = f"kubernetes.io/cluster/{_STACK_NAME}"
_TARGET = RegionalVolumeTarget(
    stack_name=_STACK_NAME,
    stack_id=_STACK_ID,
    region=_REGION,
    cluster_name=_STACK_NAME,
    cluster_tag_key=_CLUSTER_TAG_KEY,
)
_OBSERVABILITY_CONTEXT = {
    "cluster_observability": {
        "enabled": True,
        "prometheus": {"persistence_size": "50Gi"},
        "alertmanager": {"enabled": True, "persistence_size": "5Gi"},
    }
}


def _pvc(
    *,
    name: str,
    namespace: str = "gco-monitoring",
    uid: str,
    size: str,
    phase: str = "Bound",
    volume_name: str | None = None,
    component: str | None = None,
) -> dict[str, Any]:
    metadata: dict[str, Any] = {"namespace": namespace, "name": name, "uid": uid}
    if component is not None:
        metadata["labels"] = {"app.kubernetes.io/name": component}
    spec: dict[str, Any] = {
        "storageClassName": "gp3",
        "resources": {"requests": {"storage": size}},
    }
    if volume_name is not None:
        spec["volumeName"] = volume_name
    return {"metadata": metadata, "spec": spec, "status": {"phase": phase}}


def _pv(
    *,
    name: str,
    uid: str,
    driver: str | None = "ebs.csi.aws.com",
    handle: str | None = "vol-0aaa",
) -> dict[str, Any]:
    csi: dict[str, Any] = {}
    if driver is not None:
        csi["driver"] = driver
    if handle is not None:
        csi["volumeHandle"] = handle
    spec: dict[str, Any] = {"csi": csi} if csi else {}
    return {"metadata": {"name": name, "uid": uid}, "spec": spec}


def _volume_dto(
    *,
    volume_id: str = "vol-0aaa",
    size: int = 50,
    state: str = "available",
    tag_key: str = _CLUSTER_TAG_KEY,
    tag_value: str = "owned",
    zone: str = f"{_REGION}a",
    attachments: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    return {
        "VolumeId": volume_id,
        "AvailabilityZone": zone,
        "Size": size,
        "State": state,
        "Tags": [{"Key": tag_key, "Value": tag_value}],
        "Attachments": attachments or [],
    }


def _settings(tmp_path: Path, **overrides: Any) -> RunSettings:
    base = RunSettings(
        run_id="run-123",
        repo_root=tmp_path,
        report_dir=tmp_path / "report",
        checkpoint_path=tmp_path / "report" / "checkpoint.json",
        expected_account=_ACCOUNT,
        expected_sha="a" * 40,
        expected_branch=_BRANCH,
        profile="configured",
        requested_actions=("all",),
        volume_scenario_case="retain-override",
    )
    return dataclasses.replace(base, **overrides) if overrides else base


def _context(settings: RunSettings, *, session: Any, **state: Any) -> RunContext:
    checkpoint = RunCheckpoint(identity=settings.identity())
    checkpoint.state.update(
        {
            "account_arn": _CALLER_ARN,
            "owned_stacks": {_REGION: {_STACK_NAME: {"stack_id": _STACK_ID}}},
            **state,
        }
    )
    report = ValidationReport(
        run_id=settings.run_id,
        identity=settings.identity(),
        selected_actions=list(settings.requested_actions),
        started_at="2026-07-17T00:00:00+00:00",
    )
    return RunContext(
        settings=settings,
        checkpoint=checkpoint,
        report=report,
        cdk_context=dict(_OBSERVABILITY_CONTEXT),
        deployment_regions=(_REGION,),
        config=SimpleNamespace(project_name=_PROJECT, global_region=_REGION),
        session=session,
        stack_manager=MagicMock(),
        aws_client=MagicMock(),
        job_manager=MagicMock(),
        persist_callback=MagicMock(),
    )


def _kubectl(pvcs: list[dict[str, Any]], pvs: list[dict[str, Any]]) -> Any:
    def runner(*args: str, timeout: int = 120) -> tuple[int, str, str]:
        resource = args[1]
        if resource == "persistentvolumeclaims":
            assert "--all-namespaces" in args
            return 0, json.dumps({"items": pvcs}), ""
        assert resource == "persistentvolumes"
        assert "--all-namespaces" not in args
        return 0, json.dumps({"items": pvs}), ""

    return runner


def _ec2_session(responses: dict[str, Any]) -> Any:
    client = MagicMock()

    def describe_volumes(*, VolumeIds: list[str]) -> dict[str, Any]:  # noqa: N803
        outcome = responses[VolumeIds[0]]
        if isinstance(outcome, BaseException):
            raise outcome
        assert isinstance(outcome, dict)
        return outcome

    client.describe_volumes.side_effect = describe_volumes
    session = MagicMock()
    session.client.return_value = client
    return session


class TestPvcRecordBuilding:
    """Identity comes from live objects, with an explicit reason otherwise."""

    def test_bound_ebs_pvc_records_full_identity(self) -> None:
        records = checks_volumes.pvc_records(
            [
                _pvc(
                    name="prometheus-db",
                    uid="pvc-uid-1",
                    size="50Gi",
                    volume_name="pv-1",
                    component="prometheus",
                )
            ],
            [_pv(name="pv-1", uid="pv-uid-1", handle="vol-0aaa")],
        )

        assert records == [
            {
                "namespace": "gco-monitoring",
                "name": "prometheus-db",
                "uid": "pvc-uid-1",
                "requested_size": "50Gi",
                "requested_size_gib": 50,
                "phase": "Bound",
                "storage_class": "gp3",
                "component": "prometheus",
                "volume_name": "pv-1",
                "persistent_volume": {
                    "name": "pv-1",
                    "uid": "pv-uid-1",
                    "csi_driver": "ebs.csi.aws.com",
                    "volume_handle": "vol-0aaa",
                },
                "volume_id": "vol-0aaa",
                "participating": True,
                "reason_code": None,
                "reason": None,
            }
        ]

    @pytest.mark.parametrize(
        ("pvc_kwargs", "pvs", "reason_code"),
        [
            ({"phase": "Pending"}, [], "pvc-not-bound"),
            ({"volume_name": "pv-missing"}, [], "persistent-volume-absent"),
            (
                {"volume_name": "pv-1"},
                [_pv(name="pv-1", uid="u", driver="efs.csi.aws.com")],
                "persistent-volume-not-ebs-csi",
            ),
            (
                {"volume_name": "pv-1"},
                [_pv(name="pv-1", uid="u", handle=None)],
                "persistent-volume-missing-volume-handle",
            ),
            (
                {"volume_name": "pv-1"},
                [_pv(name="pv-1", uid="u", handle="fs-0123")],
                "volume-handle-is-not-an-ebs-volume-id",
            ),
        ],
    )
    def test_non_participating_pvcs_record_a_reason(
        self,
        pvc_kwargs: dict[str, Any],
        pvs: list[dict[str, Any]],
        reason_code: str,
    ) -> None:
        records = checks_volumes.pvc_records(
            [_pvc(name="claim", uid="uid", size="5Gi", **pvc_kwargs)],
            pvs,
        )

        assert records[0]["participating"] is False
        assert records[0]["reason_code"] == reason_code
        assert records[0]["reason"]
        assert records[0]["volume_id"] is None

    def test_one_bad_pvc_does_not_hide_the_others(self) -> None:
        records = checks_volumes.pvc_records(
            [
                _pvc(name="a-broken", uid="u1", size="5Gi", phase="Pending"),
                _pvc(name="b-good", uid="u2", size="5Gi", volume_name="pv-2"),
            ],
            [_pv(name="pv-2", uid="pv-uid-2", handle="vol-0bbb")],
        )

        assert [record["name"] for record in records] == ["a-broken", "b-good"]
        assert [record["participating"] for record in records] == [False, True]

    @pytest.mark.parametrize(
        ("value", "expected"),
        [("50Gi", 50), ("5Gi", 5), ("1Ti", 1024), (f"{1024**3}", 1), ("500Mi", None), ("", None)],
    )
    def test_requested_sizes_are_read_not_inferred(self, value: str, expected: int | None) -> None:
        assert checks_volumes.quantity_to_gib(value) == expected

    def test_malformed_kubectl_output_fails_closed(self) -> None:
        def runner(*args: str, timeout: int = 120) -> tuple[int, str, str]:
            return 1, "", "connection refused"

        with pytest.raises(RuntimeError, match="failed with exit code 1"):
            checks_volumes.read_volume_objects(runner)


class TestRecordedVolumeObservations:
    """EBS facts come from the shared production normalization only."""

    def test_in_scope_volume_records_normalized_facts(self) -> None:
        session = _ec2_session({"vol-0aaa": {"Volumes": [_volume_dto()]}})

        observations = checks_volumes.describe_recorded_volumes(
            session, target=_TARGET, volume_ids=["vol-0aaa"]
        )

        assert observations["vol-0aaa"] == {
            "volume_id": "vol-0aaa",
            "region": _REGION,
            "availability_zone": f"{_REGION}a",
            "size_gib": 50,
            "state": "available",
            "cluster_tag_key": _CLUSTER_TAG_KEY,
            "cluster_tag_value": "owned",
            "attachment_ids": [],
            "observed": True,
        }
        session.client.assert_called_once_with("ec2", region_name=_REGION)

    def test_attached_volume_records_its_attachments_and_state(self) -> None:
        session = _ec2_session(
            {
                "vol-0aaa": {
                    "Volumes": [
                        _volume_dto(
                            state="in-use",
                            attachments=[{"InstanceId": "i-0123", "VolumeId": "vol-0aaa"}],
                        )
                    ]
                }
            }
        )

        observation = checks_volumes.describe_recorded_volumes(
            session, target=_TARGET, volume_ids=["vol-0aaa"]
        )["vol-0aaa"]

        assert observation["state"] == "in-use"
        assert observation["attachment_ids"] == ["i-0123"]

    @pytest.mark.parametrize(
        ("outcome", "reason_code"),
        [
            (
                ClientError({"Error": {"Code": "InvalidVolume.NotFound"}}, "DescribeVolumes"),
                "ebs-volume-absent",
            ),
            (
                ClientError({"Error": {"Code": "RequestLimitExceeded"}}, "DescribeVolumes"),
                "ebs-describe-error",
            ),
            ({"Volumes": []}, "ebs-volume-ambiguous"),
            ({"Volumes": [_volume_dto(tag_key="kubernetes.io/cluster/other")]}, None),
            ({"Volumes": [_volume_dto(size=-1)]}, "ebs-normalization-error"),
        ],
    )
    def test_unobservable_volumes_record_a_reason(
        self,
        outcome: Any,
        reason_code: str | None,
    ) -> None:
        session = _ec2_session({"vol-0aaa": outcome})

        observation = checks_volumes.describe_recorded_volumes(
            session, target=_TARGET, volume_ids=["vol-0aaa"]
        )["vol-0aaa"]

        assert observation["observed"] is False
        expected = reason_code or "ebs-volume-outside-target-scope"
        assert observation["reason_code"] == expected


class TestObservabilitySizeAssertions:
    """Prometheus and Alertmanager are asserted separately, on observed state."""

    def _records(self, prometheus_gib: int, alertmanager_gib: int) -> list[dict[str, Any]]:
        return checks_volumes.pvc_records(
            [
                _pvc(
                    name="prometheus-db",
                    uid="u1",
                    size=f"{prometheus_gib}Gi",
                    volume_name="pv-1",
                    component="prometheus",
                ),
                _pvc(
                    name="alertmanager-db",
                    uid="u2",
                    size=f"{alertmanager_gib}Gi",
                    volume_name="pv-2",
                    component="alertmanager",
                ),
            ],
            [
                _pv(name="pv-1", uid="pv-u1", handle="vol-0aaa"),
                _pv(name="pv-2", uid="pv-u2", handle="vol-0bbb"),
            ],
        )

    def _observations(self, prometheus_gib: int, alertmanager_gib: int) -> dict[str, Any]:
        return {
            "vol-0aaa": {"volume_id": "vol-0aaa", "size_gib": prometheus_gib, "observed": True},
            "vol-0bbb": {"volume_id": "vol-0bbb", "size_gib": alertmanager_gib, "observed": True},
        }

    def test_observed_defaults_are_verified_per_component(self) -> None:
        assertions = checks_volumes.observability_size_assertions(
            self._records(50, 5),
            self._observations(50, 5),
            cdk_context=_OBSERVABILITY_CONTEXT,
        )

        assert assertions["prometheus"]["expected_size_gib"] == 50
        assert assertions["alertmanager"]["expected_size_gib"] == 5
        assert assertions["prometheus"]["status"] == "verified"
        assert assertions["alertmanager"]["status"] == "verified"
        assert assertions["prometheus"]["pvcs"][0]["volume_id"] == "vol-0aaa"
        assert assertions["alertmanager"]["pvcs"][0]["ebs_size_gib"] == 5

    def test_one_component_size_change_fails_only_that_component(self) -> None:
        assertions = checks_volumes.observability_size_assertions(
            self._records(50, 20),
            self._observations(50, 20),
            cdk_context=_OBSERVABILITY_CONTEXT,
        )

        assert assertions["prometheus"]["status"] == "verified"
        assert assertions["alertmanager"]["status"] == "failed"
        assert any(
            "expected 5 GiB" in failure for failure in assertions["alertmanager"]["failures"]
        )

    def test_missing_component_pvc_is_a_failure_not_a_guess(self) -> None:
        assertions = checks_volumes.observability_size_assertions(
            [],
            {},
            cdk_context=_OBSERVABILITY_CONTEXT,
        )

        assert assertions["prometheus"]["status"] == "failed"
        assert "component label" in assertions["prometheus"]["failures"][0]


class TestVolumeInventoryAction:
    """The action is fenced, ordered, and durable."""

    def _run(
        self,
        ctx: RunContext,
        *,
        pvcs: list[dict[str, Any]],
        pvs: list[dict[str, Any]],
    ) -> dict[str, Any]:
        from contextlib import contextmanager

        @contextmanager
        def fake_session(cluster_name: str, region: str) -> Any:
            assert cluster_name == _STACK_NAME
            assert region == _REGION
            yield _kubectl(pvcs, pvs)

        with (
            patch.object(volume_inventory, "cluster_kubectl", fake_session),
            patch.object(ownership_volumes, "_resolve_branch", return_value=_BRANCH),
            patch.object(ownership_volumes, "_authorize_owned_stack") as authorize_stack,
        ):
            result = volume_inventory.action_volume_inventory(ctx)
        self.authorize_stack = authorize_stack
        return result

    def test_disabled_scenario_is_a_recorded_no_op(self, tmp_path: Path) -> None:
        session = MagicMock()
        ctx = _context(_settings(tmp_path, volume_scenario_case="disabled"), session=session)

        result = volume_inventory.action_volume_inventory(ctx)

        assert result["status"] == "skipped"
        assert "--volume-scenario" in result["reason"]
        assert "volume_scenario" not in ctx.checkpoint.state
        session.client.assert_not_called()

    def test_recorded_inventory_is_complete_and_persisted(self, tmp_path: Path) -> None:
        session = _ec2_session(
            {
                "vol-0aaa": {"Volumes": [_volume_dto(volume_id="vol-0aaa", size=50)]},
                "vol-0bbb": {"Volumes": [_volume_dto(volume_id="vol-0bbb", size=5)]},
            }
        )
        ctx = _context(_settings(tmp_path), session=session)

        result = self._run(
            ctx,
            pvcs=[
                _pvc(
                    name="prometheus-db",
                    uid="u1",
                    size="50Gi",
                    volume_name="pv-1",
                    component="prometheus",
                ),
                _pvc(
                    name="alertmanager-db",
                    uid="u2",
                    size="5Gi",
                    volume_name="pv-2",
                    component="alertmanager",
                ),
                _pvc(name="unbound", uid="u3", size="5Gi", phase="Pending"),
            ],
            pvs=[
                _pv(name="pv-1", uid="pv-u1", handle="vol-0aaa"),
                _pv(name="pv-2", uid="pv-u2", handle="vol-0bbb"),
            ],
        )

        assert result["status"] == "recorded"
        evidence = result["regions"][_REGION]
        assert evidence["stack_id"] == _STACK_ID
        assert evidence["cluster_tag_key"] == _CLUSTER_TAG_KEY
        assert evidence["volume_ids"] == ["vol-0aaa", "vol-0bbb"]
        assert [volume["cluster_tag_value"] for volume in evidence["volumes"]] == ["owned", "owned"]
        assert [item["reason_code"] for item in evidence["non_participating"]] == ["pvc-not-bound"]
        assert evidence["observability"]["components"]["prometheus"]["status"] == "verified"
        assert evidence["observability"]["components"]["alertmanager"]["status"] == "verified"
        persisted = ctx.checkpoint.state["volume_scenario"]["pre_destroy_inventory"]
        assert persisted is result
        assert ctx.persist_callback.call_count >= 3  # type: ignore[attr-defined]

    def test_authorization_precedes_every_cluster_and_ec2_request(self, tmp_path: Path) -> None:
        inner = _ec2_session({"vol-0aaa": {"Volumes": [_volume_dto()]}})
        ctx = _context(_settings(tmp_path), session=MagicMock())
        ctx.cdk_context["cluster_observability"] = {"enabled": False}
        order: list[str] = []

        from contextlib import contextmanager

        @contextmanager
        def fake_session(cluster_name: str, region: str) -> Any:
            order.append("kubectl")
            yield _kubectl(
                [_pvc(name="claim", uid="u1", size="50Gi", volume_name="pv-1")],
                [_pv(name="pv-1", uid="pv-u1", handle="vol-0aaa")],
            )

        def authorize(*args: Any, **kwargs: Any) -> None:
            order.append("authorize-stack")

        def ec2_client(*args: Any, **kwargs: Any) -> Any:
            order.append("ec2")
            return inner.client("ec2", region_name=_REGION)

        ctx.session.client.side_effect = ec2_client

        with (
            patch.object(volume_inventory, "cluster_kubectl", fake_session),
            patch.object(ownership_volumes, "_resolve_branch", return_value=_BRANCH),
            patch.object(ownership_volumes, "_authorize_owned_stack", side_effect=authorize),
        ):
            result = volume_inventory.action_volume_inventory(ctx)

        assert order == ["authorize-stack", "kubectl", "ec2"]
        assert result["regions"][_REGION]["volume_ids"] == ["vol-0aaa"]

    def test_missing_stack_identity_blocks_before_any_request(self, tmp_path: Path) -> None:
        session = MagicMock()
        ctx = _context(_settings(tmp_path), session=session, owned_stacks={})

        with pytest.raises(RuntimeError, match="No checkpointed CloudFormation identity"):
            self._run(ctx, pvcs=[], pvs=[])

        session.client.assert_not_called()
        failed = ctx.checkpoint.state["volume_scenario"]["pre_destroy_inventory"]
        assert failed["status"] == "failed"
        assert failed["regions"][_REGION]["result"] == "failed"

    def test_absent_ebs_volume_is_recorded_and_validation_continues(self, tmp_path: Path) -> None:
        session = _ec2_session(
            {
                "vol-0aaa": {"Volumes": [_volume_dto(volume_id="vol-0aaa", size=50)]},
                "vol-0bbb": ClientError(
                    {"Error": {"Code": "InvalidVolume.NotFound"}}, "DescribeVolumes"
                ),
            }
        )
        ctx = _context(_settings(tmp_path), session=session)

        with pytest.raises(RuntimeError, match="do not match their configured sizes"):
            self._run(
                ctx,
                pvcs=[
                    _pvc(
                        name="prometheus-db",
                        uid="u1",
                        size="50Gi",
                        volume_name="pv-1",
                        component="prometheus",
                    ),
                    _pvc(
                        name="alertmanager-db",
                        uid="u2",
                        size="5Gi",
                        volume_name="pv-2",
                        component="alertmanager",
                    ),
                ],
                pvs=[
                    _pv(name="pv-1", uid="pv-u1", handle="vol-0aaa"),
                    _pv(name="pv-2", uid="pv-u2", handle="vol-0bbb"),
                ],
            )

        evidence = ctx.checkpoint.state["volume_scenario"]["pre_destroy_inventory"]["regions"][
            _REGION
        ]
        assert evidence["volume_ids"] == ["vol-0aaa"]
        assert [item["reason_code"] for item in evidence["non_participating"]] == [
            "ebs-volume-absent"
        ]
        prometheus = evidence["observability"]["components"]["prometheus"]
        assert prometheus["status"] == "verified"

    def test_disabled_observability_records_no_size_assertions(self, tmp_path: Path) -> None:
        session = _ec2_session({"vol-0aaa": {"Volumes": [_volume_dto()]}})
        ctx = _context(_settings(tmp_path), session=session)
        ctx.cdk_context["cluster_observability"] = {"enabled": False}

        result = self._run(
            ctx,
            pvcs=[_pvc(name="claim", uid="u1", size="50Gi", volume_name="pv-1")],
            pvs=[_pv(name="pv-1", uid="pv-u1", handle="vol-0aaa")],
        )

        evidence = result["regions"][_REGION]
        assert evidence["observability"]["enabled"] is False
        assert evidence["observability"]["components"] == {}
        assert evidence["volume_ids"] == ["vol-0aaa"]


class TestRegistryLockstep:
    """The action is registered after topology and documented identically."""

    def test_action_runs_immediately_after_topology(self) -> None:
        names = list(build_action_registry())

        assert names[names.index("topology") + 1] == "volume-inventory"
        assert build_action_registry()["volume-inventory"].dependencies == ("topology",)

    def test_handler_is_the_action_module_handler(self) -> None:
        definition = build_action_registry()["volume-inventory"]

        assert definition.handler is volume_inventory.action_volume_inventory
        assert (definition.handler.__doc__ or "").strip()
