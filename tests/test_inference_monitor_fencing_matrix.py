"""Lease, provenance, and deletion-quorum tests for the inference monitor."""

from __future__ import annotations

import ast
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from kubernetes.client.rest import ApiException

import gco.services.inference_monitor as inference_monitor
from gco.services.inference_monitor import (
    PASS_STALL_INTERVALS,
    InferenceMonitor,
    ReconcileAuthority,
    ReconcileFencedError,
)


def _monitor(store: Any | None = None) -> InferenceMonitor:
    with (
        patch("gco.services.inference_monitor.config.load_incluster_config"),
        patch("gco.services.inference_monitor.client.AppsV1Api"),
        patch("gco.services.inference_monitor.client.CoreV1Api"),
        patch("gco.services.inference_monitor.client.NetworkingV1Api"),
        patch("gco.services.inference_monitor.client.AutoscalingV2Api"),
    ):
        return InferenceMonitor(
            cluster_id="gco-us-east-1",
            region="us-east-1",
            store=store or MagicMock(),
            namespace="gco-inference",
            reconcile_interval=5,
        )


def _lease(
    *,
    holder: str = "pod-a",
    epoch: str = "epoch-a",
    renew_time: datetime | None = None,
    duration: int = 15,
) -> SimpleNamespace:
    return SimpleNamespace(
        metadata=SimpleNamespace(annotations={"gco.io/leader-epoch": epoch}),
        spec=SimpleNamespace(
            holder_identity=holder,
            renew_time=renew_time or datetime.now(UTC),
            acquire_time=datetime.now(UTC),
            lease_duration_seconds=duration,
            lease_transitions=1,
        ),
    )


def _authority(**overrides: Any) -> ReconcileAuthority:
    values: dict[str, Any] = {
        "endpoint_name": "chat",
        "lifecycle_id": "life-1",
        "region_generation": "region-1",
        "leader_epoch": "epoch-a",
    }
    values.update(overrides)
    return ReconcileAuthority(**values)


def _resource(
    *,
    annotations: dict[str, str] | None = None,
    labels: dict[str, str] | None = None,
    uid: str | None = "uid-1",
    version: str | None = "7",
) -> dict[str, Any]:
    return {
        "metadata": {
            "annotations": annotations or {},
            "labels": labels or {"project": "gco", "gco.io/type": "inference"},
            "uid": uid,
            "resourceVersion": version,
        }
    }


class _Ticks:
    """A stop event whose ``wait`` returns at once: ``count`` intervals pass, then it is set."""

    def __init__(self, count: int) -> None:
        self.count = count

    def wait(self, _timeout: float) -> bool:
        self.count -= 1
        return self.count < 0


class TestWedgedPassReleasesLeadership:
    """Finding I9: a pass that stops progressing must not hold the Lease forever."""

    def test_stall_deadline_is_ten_reconcile_intervals(self) -> None:
        monitor = _monitor()

        assert monitor._pass_stall_seconds == PASS_STALL_INTERVALS * 5 == 50

    def test_renewal_stops_once_the_pass_makes_no_progress(self) -> None:
        monitor = _monitor()
        monitor._lease_name, monitor._lease_holder, monitor._leader_epoch = (
            "leader",
            "pod-a",
            "epoch-a",
        )
        monitor._pass_progress_at = time.monotonic() - monitor._pass_stall_seconds - 1
        coordination = MagicMock()
        coordination.read_namespaced_lease.return_value = _lease()

        with patch(
            "gco.services.inference_monitor.client.CoordinationV1Api", return_value=coordination
        ):
            monitor._lease_renewal_loop(_Ticks(3))  # type: ignore[arg-type]

        coordination.replace_namespaced_lease.assert_not_called()
        assert monitor._leadership_lost.is_set()
        with pytest.raises(ReconcileFencedError, match="Lease was lost"):
            monitor._assert_current_leadership()

    def test_renewal_continues_while_the_pass_progresses(self) -> None:
        monitor = _monitor()
        monitor._lease_name, monitor._lease_holder, monitor._leader_epoch = (
            "leader",
            "pod-a",
            "epoch-a",
        )
        coordination = MagicMock()
        coordination.read_namespaced_lease.return_value = _lease()

        with patch(
            "gco.services.inference_monitor.client.CoordinationV1Api", return_value=coordination
        ):
            with monitor._renewing_leadership():
                pass  # the pass starts with fresh progress
            monitor._lease_renewal_loop(_Ticks(2))  # type: ignore[arg-type]

        assert coordination.replace_namespaced_lease.call_count == 2
        assert not monitor._leadership_lost.is_set()

    @pytest.mark.asyncio
    async def test_every_endpoint_and_purge_step_marks_progress(self) -> None:
        store = MagicMock()
        store.list_endpoints.return_value = [
            {"endpoint_name": "a", "desired_state": "running"},
            {"endpoint_name": "b", "desired_state": "deleted"},
        ]
        store.get_endpoint.return_value = None
        monitor = _monitor(store)
        monitor._reconcile_endpoint = AsyncMock(return_value=None)  # type: ignore[method-assign]
        monitor._mark_pass_progress = MagicMock()  # type: ignore[method-assign]

        await monitor.reconcile()

        # One per reconciled endpoint, one per deleted endpoint's purge check.
        assert monitor._mark_pass_progress.call_count == 3

    def test_lease_calls_are_bounded_by_the_api_timeout(self) -> None:
        monitor = _monitor()
        timeout = monitor._k8s_timeout
        coordination = MagicMock()
        coordination.read_namespaced_lease.return_value = _lease()
        with patch(
            "gco.services.inference_monitor.client.CoordinationV1Api", return_value=coordination
        ):
            assert monitor._try_acquire_lease("leader", "pod-a") is True
            assert monitor._renew_current_lease() is True
            monitor._assert_current_leadership()
            coordination.read_namespaced_lease.side_effect = ApiException(status=404)
            monitor._leader_epoch = None
            assert monitor._try_acquire_lease("leader", "pod-a") is True

        calls = [
            *coordination.read_namespaced_lease.call_args_list,
            *coordination.replace_namespaced_lease.call_args_list,
            *coordination.create_namespaced_lease.call_args_list,
        ]
        assert len(calls) == 7
        assert all(call.kwargs.get("_request_timeout") == timeout for call in calls)

    def test_every_namespaced_kubernetes_call_names_a_timeout(self) -> None:
        """No call in the monitor may block forever: the client sets no default timeout."""
        source = Path(inference_monitor.__file__).read_text(encoding="utf-8")
        verbs = ("read_", "create_", "replace_", "patch_", "delete_", "list_", "get_namespaced")
        unbounded = [
            f"{node.lineno}:{node.func.attr}"
            for node in ast.walk(ast.parse(source))
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr.startswith(verbs)
            and "namespaced" in node.func.attr
            and "_request_timeout" not in {keyword.arg for keyword in node.keywords}
        ]

        assert unbounded == []


class TestLeaseEpochFencing:
    def test_same_holder_adopts_persisted_epoch_after_local_restart(self) -> None:
        monitor = _monitor()
        coordination = MagicMock()
        lease = _lease(epoch="persisted")
        coordination.read_namespaced_lease.return_value = lease
        with (
            patch(
                "gco.services.inference_monitor.client.CoordinationV1Api",
                return_value=coordination,
            ),
            patch("gco.services.inference_monitor.secrets.token_hex") as token,
        ):
            assert monitor._try_acquire_lease("leader", "pod-a") is True
        assert monitor._leader_epoch == "persisted"
        assert lease.metadata.annotations["gco.io/leader-epoch"] == "persisted"
        token.assert_not_called()
        coordination.replace_namespaced_lease.assert_called_once_with(
            "leader", "gco-inference", lease, _request_timeout=monitor._k8s_timeout
        )

    def test_same_holder_with_different_local_epoch_loses_authority(self) -> None:
        monitor = _monitor()
        monitor._leader_epoch = "local"
        coordination = MagicMock()
        coordination.read_namespaced_lease.return_value = _lease(epoch="persisted")
        with patch(
            "gco.services.inference_monitor.client.CoordinationV1Api",
            return_value=coordination,
        ):
            assert monitor._try_acquire_lease("leader", "pod-a") is False
        assert monitor._leadership_lost.is_set()
        assert monitor._leader_epoch == "local"
        coordination.replace_namespaced_lease.assert_not_called()

    def test_naive_renew_time_and_invalid_duration_use_safe_defaults(self) -> None:
        monitor = _monitor()
        now = datetime.now(UTC)
        lease = _lease(renew_time=(now - timedelta(seconds=20)).replace(tzinfo=None))
        lease.spec.lease_duration_seconds = True
        assert monitor._lease_is_expired(lease, now) is True
        lease.spec.renew_time = None
        assert monitor._lease_is_expired(lease, now) is True

    def test_renew_requires_complete_local_identity(self) -> None:
        monitor = _monitor()
        with patch("gco.services.inference_monitor.client.CoordinationV1Api") as coordination:
            assert monitor._renew_current_lease() is False
        coordination.assert_not_called()

    @pytest.mark.parametrize(
        ("holder", "epoch", "expired"),
        [
            ("other", "epoch-a", False),
            ("pod-a", "other", False),
            ("pod-a", "epoch-a", True),
        ],
    )
    def test_renew_rejects_changed_or_expired_persisted_lease(
        self, holder: str, epoch: str, expired: bool
    ) -> None:
        monitor = _monitor()
        monitor._lease_name = "leader"
        monitor._lease_holder = "pod-a"
        monitor._leader_epoch = "epoch-a"
        coordination = MagicMock()
        renew_time = datetime.now(UTC) - timedelta(minutes=1) if expired else datetime.now(UTC)
        coordination.read_namespaced_lease.return_value = _lease(
            holder=holder, epoch=epoch, renew_time=renew_time
        )
        with patch(
            "gco.services.inference_monitor.client.CoordinationV1Api",
            return_value=coordination,
        ):
            assert monitor._renew_current_lease() is False
        coordination.replace_namespaced_lease.assert_not_called()

    def test_renew_updates_only_matching_holder_epoch(self) -> None:
        monitor = _monitor()
        monitor._lease_name = "leader"
        monitor._lease_holder = "pod-a"
        monitor._leader_epoch = "epoch-a"
        coordination = MagicMock()
        lease = _lease()
        old = lease.spec.renew_time
        coordination.read_namespaced_lease.return_value = lease
        with patch(
            "gco.services.inference_monitor.client.CoordinationV1Api",
            return_value=coordination,
        ):
            assert monitor._renew_current_lease() is True
        assert lease.spec.renew_time >= old
        coordination.replace_namespaced_lease.assert_called_once_with(
            "leader", "gco-inference", lease, _request_timeout=monitor._k8s_timeout
        )

    @pytest.mark.parametrize("operation", ["read", "replace"])
    def test_renew_api_failures_do_not_escape(self, operation: str) -> None:
        monitor = _monitor()
        monitor._lease_name = "leader"
        monitor._lease_holder = "pod-a"
        monitor._leader_epoch = "epoch-a"
        coordination = MagicMock()
        if operation == "read":
            coordination.read_namespaced_lease.side_effect = RuntimeError("offline")
        else:
            coordination.read_namespaced_lease.return_value = _lease()
            coordination.replace_namespaced_lease.side_effect = RuntimeError("conflict")
        with patch(
            "gco.services.inference_monitor.client.CoordinationV1Api",
            return_value=coordination,
        ):
            assert monitor._renew_current_lease() is False

    def test_current_leadership_reads_and_accepts_exact_lease(self) -> None:
        monitor = _monitor()
        monitor._lease_name = "leader"
        monitor._lease_holder = "pod-a"
        monitor._leader_epoch = "epoch-a"
        coordination = MagicMock()
        coordination.read_namespaced_lease.return_value = _lease()
        with patch(
            "gco.services.inference_monitor.client.CoordinationV1Api",
            return_value=coordination,
        ):
            monitor._assert_current_leadership()
        assert not monitor._leadership_lost.is_set()

    @pytest.mark.parametrize(
        "lease_or_error",
        [
            RuntimeError("offline"),
            _lease(holder="other"),
            _lease(epoch="other"),
            _lease(renew_time=datetime.now(UTC) - timedelta(minutes=1)),
        ],
    )
    def test_current_leadership_failure_sets_local_fence(self, lease_or_error: Any) -> None:
        monitor = _monitor()
        monitor._lease_name = "leader"
        monitor._lease_holder = "pod-a"
        monitor._leader_epoch = "epoch-a"
        coordination = MagicMock()
        if isinstance(lease_or_error, Exception):
            coordination.read_namespaced_lease.side_effect = lease_or_error
            message = "could not be verified"
        else:
            coordination.read_namespaced_lease.return_value = lease_or_error
            message = "holder or epoch changed"
        with (
            patch(
                "gco.services.inference_monitor.client.CoordinationV1Api",
                return_value=coordination,
            ),
            pytest.raises(ReconcileFencedError, match=message),
        ):
            monitor._assert_current_leadership()
        assert monitor._leadership_lost.is_set()


class TestEndpointAndObjectAuthority:
    @pytest.mark.parametrize(
        ("endpoint", "message"),
        [
            ({"endpoint_name": "chat", "updated_at": "now"}, "no lifecycle authority"),
            (
                {
                    "endpoint_name": "chat",
                    "updated_at": "now",
                    "lifecycle_id": "life-1",
                },
                "no Region generation",
            ),
        ],
    )
    def test_persisted_endpoint_without_complete_authority_is_fenced(
        self, endpoint: dict[str, Any], message: str
    ) -> None:
        with pytest.raises(ReconcileFencedError, match=message):
            _monitor()._authority_from_endpoint(endpoint)

    def test_direct_fixture_without_persistence_timestamp_remains_compatible(self) -> None:
        assert _monitor()._authority_from_endpoint({"endpoint_name": "chat"}) is None

    def test_authority_records_delete_and_region_removal_modes(self) -> None:
        monitor = _monitor()
        monitor._leader_epoch = "epoch-a"
        deleted = monitor._authority_from_endpoint(
            {
                "endpoint_name": "chat",
                "lifecycle_id": "life-1",
                "region_generations": {"us-east-1": "region-1"},
                "deletion_generation": "delete-1",
                "desired_state": "deleted",
                "target_regions": ["us-east-1"],
            }
        )
        assert deleted is not None and deleted.deleting is True
        assert deleted.deletion_generation == "delete-1"
        removed = monitor._authority_from_endpoint(
            {
                "endpoint_name": "chat",
                "lifecycle_id": "life-1",
                "region_generations": {"us-east-1": "region-1"},
                "desired_state": "running",
                "target_regions": ["eu-west-1"],
            }
        )
        assert removed is not None and removed.region_removed is True

    def _authorized_monitor(self) -> InferenceMonitor:
        monitor = _monitor()
        monitor._active_authority = _authority()
        monitor._lease_name = "leader"
        monitor._lease_holder = "pod-a"
        monitor._leader_epoch = "epoch-a"
        monitor._assert_current_leadership = MagicMock()  # type: ignore[method-assign]
        return monitor

    def test_ambiguous_legacy_object_is_never_claimed(self) -> None:
        monitor = self._authorized_monitor()
        resource = _resource(labels={"app": "other"})
        with pytest.raises(ReconcileFencedError, match="ambiguous legacy ownership"):
            monitor._authorize_resource(resource, kind="deployment", resource_name="chat")

    def test_claim_requires_patch_reader_version_and_current_ddb_authority(self) -> None:
        monitor = self._authorized_monitor()
        resource = _resource()
        with pytest.raises(ReconcileFencedError, match="lacks current immutable provenance"):
            monitor._authorize_resource(resource, kind="service", resource_name="chat")
        with pytest.raises(ReconcileFencedError, match="no resourceVersion"):
            monitor._authorize_resource(
                _resource(version=None),
                kind="service",
                resource_name="chat",
                patch_metadata=MagicMock(),
                read_resource=MagicMock(),
            )
        monitor._strong_authority_matches = MagicMock(return_value=False)  # type: ignore[method-assign]
        with pytest.raises(ReconcileFencedError, match="endpoint authority changed"):
            monitor._authorize_resource(
                resource,
                kind="service",
                resource_name="chat",
                patch_metadata=MagicMock(),
                read_resource=MagicMock(),
            )

    def test_claim_patch_and_readback_fail_closed(self) -> None:
        monitor = self._authorized_monitor()
        monitor._strong_authority_matches = MagicMock(return_value=True)  # type: ignore[method-assign]
        resource = _resource()
        patch_metadata = MagicMock(side_effect=RuntimeError("conflict"))
        with pytest.raises(ReconcileFencedError, match="changed during authority claim"):
            monitor._authorize_resource(
                resource,
                kind="service",
                resource_name="chat",
                patch_metadata=patch_metadata,
                read_resource=MagicMock(),
            )
        patch_metadata = MagicMock()
        read_resource = MagicMock(return_value=_resource())
        with pytest.raises(ReconcileFencedError, match="claim could not be verified"):
            monitor._authorize_resource(
                resource,
                kind="service",
                resource_name="chat",
                patch_metadata=patch_metadata,
                read_resource=read_resource,
            )
        body = patch_metadata.call_args.kwargs["body"]["metadata"]
        assert body["resourceVersion"] == "7"
        assert body["annotations"] == monitor._active_authority.annotations

    def test_successful_claim_returns_fresh_readback(self) -> None:
        monitor = self._authorized_monitor()
        monitor._strong_authority_matches = MagicMock(return_value=True)  # type: ignore[method-assign]
        claimed = _resource(annotations=monitor._active_authority.annotations, version="8")
        patch_metadata = MagicMock()
        result = monitor._authorize_resource(
            _resource(),
            kind="service",
            resource_name="chat",
            patch_metadata=patch_metadata,
            read_resource=MagicMock(return_value=claimed),
        )
        assert result is claimed


class TestLifecycleNormalizationAndPurge:
    @staticmethod
    def _complete_endpoint(**overrides: Any) -> dict[str, Any]:
        endpoint: dict[str, Any] = {
            "endpoint_name": "chat",
            "updated_at": "v1",
            "lifecycle_id": "life-1",
            "cleanup_regions": ["us-east-1"],
            "region_generations": {"us-east-1": "region-1"},
            "desired_state": "running",
            "target_regions": ["us-east-1"],
        }
        endpoint.update(overrides)
        return endpoint

    async def test_reconcile_upgrades_legacy_snapshot_before_endpoint_work(self) -> None:
        store = MagicMock()
        legacy = {"endpoint_name": "chat", "updated_at": "v0"}
        upgraded = self._complete_endpoint()
        store.list_endpoints.return_value = [legacy]
        store.ensure_lifecycle_metadata.return_value = upgraded
        monitor = _monitor(store)
        reconcile_endpoint = AsyncMock(return_value={"action": "observe", "endpoint": "chat"})
        with patch.object(monitor, "_reconcile_endpoint", reconcile_endpoint):
            actions = await monitor.reconcile()
        assert actions == [
            {"action": "initialize_lifecycle", "endpoint": "chat"},
            {"action": "observe", "endpoint": "chat"},
        ]
        store.ensure_lifecycle_metadata.assert_called_once_with(legacy)
        reconcile_endpoint.assert_awaited_once_with(upgraded)

    async def test_failed_conditional_upgrade_skips_stale_snapshot(self) -> None:
        store = MagicMock()
        legacy = {"endpoint_name": "chat", "updated_at": "v0"}
        store.list_endpoints.return_value = [legacy]
        store.ensure_lifecycle_metadata.return_value = None
        monitor = _monitor(store)
        reconcile_endpoint = AsyncMock()
        with patch.object(monitor, "_reconcile_endpoint", reconcile_endpoint):
            assert await monitor.reconcile() == []
        reconcile_endpoint.assert_not_awaited()

    @pytest.mark.parametrize(
        "latest",
        [
            RuntimeError("read failed"),
            {"endpoint_name": "chat", "desired_state": "running"},
            {
                "endpoint_name": "chat",
                "desired_state": "deleted",
                "lifecycle_id": "life-1",
                "deletion_generation": "delete-1",
                "updated_at": "v2",
                "deletion_regions": [],
            },
            {
                "endpoint_name": "chat",
                "desired_state": "deleted",
                "lifecycle_id": "life-1",
                "deletion_generation": "delete-1",
                "updated_at": "v2",
                "deletion_regions": ["us-east-1", "us-east-1"],
            },
        ],
    )
    async def test_malformed_or_raced_delete_snapshot_never_purges(self, latest: Any) -> None:
        store = MagicMock()
        endpoint = self._complete_endpoint(
            desired_state="deleted",
            deletion_generation="delete-1",
            deletion_regions=["us-east-1"],
        )
        store.list_endpoints.return_value = [endpoint]
        if isinstance(latest, Exception):
            store.get_endpoint.side_effect = latest
        else:
            store.get_endpoint.return_value = latest
        monitor = _monitor(store)
        with patch.object(monitor, "_reconcile_endpoint", AsyncMock(return_value=None)):
            assert await monitor.reconcile() == []
        store.delete_endpoint.assert_not_called()

    async def test_conditional_delete_race_keeps_endpoint(self) -> None:
        store = MagicMock()
        endpoint = self._complete_endpoint(
            desired_state="deleted",
            deletion_generation="delete-1",
            deletion_regions=["us-east-1"],
        )
        latest = {
            **endpoint,
            "updated_at": "v2",
            "region_status": {
                "us-east-1": {
                    "state": "deleted",
                    "lifecycle_id": "life-1",
                    "deletion_generation": "delete-1",
                    "absence_observations": 2,
                }
            },
        }
        store.list_endpoints.return_value = [endpoint]
        store.get_endpoint.return_value = latest
        store.delete_endpoint.return_value = False
        monitor = _monitor(store)
        with patch.object(monitor, "_reconcile_endpoint", AsyncMock(return_value=None)):
            assert await monitor.reconcile() == []
        store.delete_endpoint.assert_called_once_with(
            "chat",
            expected_updated_at="v2",
            expected_lifecycle_id="life-1",
            expected_deletion_generation="delete-1",
        )

    @pytest.mark.parametrize(
        ("endpoint", "terminal"),
        [
            ({"region_status": {}}, False),
            (
                {
                    "lifecycle_id": "life-1",
                    "desired_state": "deleted",
                    "deletion_generation": "delete-1",
                    "region_status": {
                        "us-east-1": {
                            "state": "deleted",
                            "lifecycle_id": "other",
                            "deletion_generation": "delete-1",
                            "absence_observations": 2,
                        }
                    },
                },
                False,
            ),
            (
                {
                    "lifecycle_id": "life-1",
                    "desired_state": "deleted",
                    "deletion_generation": "delete-1",
                    "region_status": {
                        "us-east-1": {
                            "state": "deleted",
                            "lifecycle_id": "life-1",
                            "deletion_generation": "delete-1",
                            "absence_observations": 2,
                        }
                    },
                },
                True,
            ),
            (
                {
                    "lifecycle_id": "life-1",
                    "desired_state": "running",
                    "region_generations": {"us-east-1": "region-1"},
                    "region_status": {
                        "us-east-1": {
                            "state": "deleted",
                            "lifecycle_id": "life-1",
                            "region_generation": "region-1",
                            "absence_observations": 2,
                        }
                    },
                },
                True,
            ),
        ],
    )
    def test_terminal_cleanup_ack_requires_matching_generation(
        self, endpoint: dict[str, Any], terminal: bool
    ) -> None:
        assert InferenceMonitor._cleanup_ack_is_terminal(endpoint, "us-east-1") is terminal

    async def test_interrupted_delete_initializes_generation_without_kubernetes(self) -> None:
        store = MagicMock()
        monitor = _monitor(store)
        endpoint = self._complete_endpoint(
            desired_state="deleted", deletion_generation=None, deletion_regions=None
        )
        result = await monitor._reconcile_endpoint(endpoint)
        assert result == {"action": "initialize_deletion", "endpoint": "chat"}
        store.update_desired_state.assert_called_once_with(
            "chat", "deleted", expected_lifecycle_id="life-1"
        )


class TestExactUidDeleteFailures:
    def _call(
        self,
        monitor: InferenceMonitor,
        read_call: MagicMock,
        delete_call: MagicMock,
    ) -> tuple[bool, list[str], list[str]]:
        pending: list[str] = []
        errors: list[str] = []
        monitor._authorize_resource = MagicMock(side_effect=lambda resource, **_kw: resource)  # type: ignore[method-assign]
        monitor._assert_mutation_authority = MagicMock()  # type: ignore[method-assign]
        monitor._delete_options_for = MagicMock(return_value="preconditions")  # type: ignore[method-assign]
        found = monitor._delete_and_confirm(
            kind="service",
            resource_name="chat",
            delete_call=delete_call,
            read_call=read_call,
            patch_metadata=None,
            pending=pending,
            errors=errors,
        )
        return found, pending, errors

    @pytest.mark.parametrize(
        "error",
        [ApiException(status=500, reason="denied"), RuntimeError("offline")],
    )
    def test_initial_read_failure_never_deletes(self, error: Exception) -> None:
        monitor = _monitor()
        read_call = MagicMock(side_effect=error)
        delete_call = MagicMock()
        found, pending, errors = self._call(monitor, read_call, delete_call)
        assert found is False
        assert pending == []
        assert errors == [
            "read service chat failed (status 500)"
            if isinstance(error, ApiException)
            else "read service chat failed"
        ]
        delete_call.assert_not_called()

    @pytest.mark.parametrize(
        "error",
        [ApiException(status=500, reason="denied"), RuntimeError("offline")],
    )
    def test_delete_failure_is_recorded_and_confirmation_still_runs(self, error: Exception) -> None:
        monitor = _monitor()
        resource = _resource()
        read_call = MagicMock(side_effect=[resource, ApiException(status=404)])
        delete_call = MagicMock(side_effect=error)
        found, pending, errors = self._call(monitor, read_call, delete_call)
        assert found is True
        assert pending == []
        assert errors == [
            "delete service chat failed (status 500)"
            if isinstance(error, ApiException)
            else "delete service chat failed"
        ]
        assert read_call.call_count == 2

    @pytest.mark.parametrize(
        "error",
        [ApiException(status=500, reason="denied"), RuntimeError("offline")],
    )
    def test_confirmation_read_failure_keeps_cleanup_incomplete(self, error: Exception) -> None:
        monitor = _monitor()
        read_call = MagicMock(side_effect=[_resource(), error])
        found, pending, errors = self._call(monitor, read_call, MagicMock())
        assert found is True
        assert pending == []
        assert errors == [
            "read service chat failed (status 500)"
            if isinstance(error, ApiException)
            else "read service chat failed"
        ]
