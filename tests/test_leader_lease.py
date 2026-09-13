"""Single-writer Lease election shared by the health-monitor's elected duties.

``gco.services.leader_lease`` is the one implementation behind the ALB→SSM
sync and the webhook deliverer election, so its rules are pinned here once:
optimistic replace, expired or timestamp-less holders may be replaced, a
racing writer (HTTP 409) loses the cycle, and every failure fails closed.
The second half covers how the webhook dispatcher consumes it — standbys
never deliver, a new leader re-seeds its cache before delivering, and a
dispatcher built without a Lease keeps the single-replica behaviour.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from kubernetes.client.rest import ApiException

from gco.services.leader_lease import (
    LEASE_MIN_DURATION_SECONDS,
    LEASE_REQUEST_TIMEOUT,
    LeaseIdentity,
    lease_duration_from_env,
    try_acquire_lease,
)

IDENTITY = LeaseIdentity(
    name="gco-health-monitor-webhooks",
    namespace="gco-system",
    holder="health-monitor-abc",
    duration_seconds=90,
)


def _lease(holder: str | None, renew_time: datetime | None, duration: int | None = 90) -> MagicMock:
    lease = MagicMock()
    lease.spec.holder_identity = holder
    lease.spec.renew_time = renew_time
    lease.spec.lease_duration_seconds = duration
    lease.spec.lease_transitions = 2
    return lease


def _api(lease: MagicMock) -> MagicMock:
    api = MagicMock()
    api.read_namespaced_lease.return_value = lease
    return api


class TestTryAcquireLease:
    def test_unheld_lease_is_acquired_with_a_transition(self):
        lease = _lease(None, None)
        api = _api(lease)

        assert try_acquire_lease(api, IDENTITY, label="webhook") is True

        assert lease.spec.holder_identity == IDENTITY.holder
        assert lease.spec.lease_transitions == 3
        assert lease.spec.lease_duration_seconds == 90
        assert lease.spec.acquire_time is not None
        api.read_namespaced_lease.assert_called_once_with(
            IDENTITY.name, IDENTITY.namespace, _request_timeout=LEASE_REQUEST_TIMEOUT
        )
        api.replace_namespaced_lease.assert_called_once_with(
            IDENTITY.name, IDENTITY.namespace, lease, _request_timeout=LEASE_REQUEST_TIMEOUT
        )

    def test_current_holder_renews_without_a_transition(self):
        acquired_at = datetime.now(UTC) - timedelta(seconds=30)
        lease = _lease(IDENTITY.holder, acquired_at)
        lease.spec.acquire_time = acquired_at
        api = _api(lease)

        assert try_acquire_lease(api, IDENTITY, label="webhook") is True

        assert lease.spec.lease_transitions == 2
        assert lease.spec.acquire_time == acquired_at
        assert lease.spec.renew_time > acquired_at

    def test_live_foreign_holder_blocks_without_writing(self):
        api = _api(_lease("other-replica", datetime.now(UTC)))

        assert try_acquire_lease(api, IDENTITY, label="webhook") is False

        api.replace_namespaced_lease.assert_not_called()

    def test_expired_foreign_holder_is_replaced(self):
        lease = _lease("dead-replica", datetime.now(UTC) - timedelta(minutes=5))
        api = _api(lease)

        assert try_acquire_lease(api, IDENTITY, label="webhook") is True
        assert lease.spec.holder_identity == IDENTITY.holder
        assert lease.spec.lease_transitions == 3

    def test_naive_renew_time_is_read_as_utc(self):
        lease = _lease("other-replica", datetime.now(UTC).replace(tzinfo=None))
        api = _api(lease)

        assert try_acquire_lease(api, IDENTITY, label="webhook") is False

    def test_holder_without_renew_time_is_treated_as_expired(self):
        lease = _lease("wedged-replica", None)
        api = _api(lease)

        assert try_acquire_lease(api, IDENTITY, label="webhook") is True
        assert lease.spec.holder_identity == IDENTITY.holder

    def test_missing_lease_duration_falls_back_to_the_identity(self):
        # 89 seconds ago with a 90 second lease: still live under the fallback.
        lease = _lease("other-replica", datetime.now(UTC) - timedelta(seconds=89), duration=None)
        api = _api(lease)

        assert try_acquire_lease(api, IDENTITY, label="webhook") is False

    def test_lost_race_returns_false(self, caplog):
        api = _api(_lease(None, None))
        api.replace_namespaced_lease.side_effect = ApiException(status=409)

        with caplog.at_level("DEBUG", logger="gco.services.leader_lease"):
            assert try_acquire_lease(api, IDENTITY, label="webhook") is False
        assert "Lost webhook Lease race" in caplog.text

    def test_replace_failure_other_than_conflict_fails_closed(self, caplog):
        api = _api(_lease(None, None))
        api.replace_namespaced_lease.side_effect = ApiException(status=500)

        with caplog.at_level("WARNING"):
            assert try_acquire_lease(api, IDENTITY, label="webhook") is False
        assert "webhook Lease check failed" in caplog.text

    def test_missing_precreated_lease_disables_the_duty(self, caplog):
        api = MagicMock()
        api.read_namespaced_lease.side_effect = ApiException(status=404)

        with caplog.at_level("WARNING"):
            assert try_acquire_lease(api, IDENTITY, label="webhook") is False

        assert "gco-system/gco-health-monitor-webhooks is missing" in caplog.text
        api.create_namespaced_lease.assert_not_called()

    def test_unexpected_error_fails_closed(self, caplog):
        api = MagicMock()
        api.read_namespaced_lease.side_effect = RuntimeError("socket closed")

        with caplog.at_level("WARNING"):
            assert try_acquire_lease(api, IDENTITY, label="ALB-sync") is False
        assert "ALB-sync Lease check failed (non-fatal): socket closed" in caplog.text


class TestLeaseDurationFromEnv:
    def test_configured_value_is_kept_when_above_the_floor(self):
        assert lease_duration_from_env("120", label="WEBHOOK_LEASE_DURATION") == 120

    def test_missing_value_uses_the_floor(self):
        assert lease_duration_from_env(None, label="X") == LEASE_MIN_DURATION_SECONDS

    def test_short_value_is_clamped_with_a_warning(self, caplog):
        with caplog.at_level("WARNING"):
            assert lease_duration_from_env("5", label="WEBHOOK_LEASE_DURATION") == 60
        assert "WEBHOOK_LEASE_DURATION=5 is too short" in caplog.text


# ---------------------------------------------------------------------------
# The webhook dispatcher as a consumer of the election
# ---------------------------------------------------------------------------


def _dispatcher(leader_lease: LeaseIdentity | None):
    from gco.services.webhook_dispatcher import WebhookDispatcher

    with (
        patch("gco.services.webhook_dispatcher.config.load_incluster_config"),
        patch("gco.services.webhook_dispatcher.client.BatchV1Api"),
        patch("gco.services.webhook_dispatcher.client.CoordinationV1Api"),
    ):
        return WebhookDispatcher(
            cluster_id="test-cluster",
            region="us-east-1",
            webhook_store=MagicMock(),
            namespaces=["gco-jobs"],
            leader_lease=leader_lease,
            standby_poll_seconds=0,
        )


class TestDispatcherElection:
    @pytest.mark.asyncio
    async def test_without_a_lease_the_dispatcher_is_always_the_deliverer(self):
        dispatcher = _dispatcher(None)
        dispatcher._initialize_job_cache = AsyncMock()

        assert await dispatcher._acquire_leadership() is True

        dispatcher._initialize_job_cache.assert_not_awaited()
        assert dispatcher.get_metrics()["leader"] is True

    @pytest.mark.asyncio
    async def test_start_seeds_the_cache_only_without_a_lease(self):
        for leader_lease, seeded in ((None, 1), (IDENTITY, 0)):
            dispatcher = _dispatcher(leader_lease)
            dispatcher._initialize_job_cache = AsyncMock()
            dispatcher._watch_jobs = AsyncMock()

            await dispatcher.start()
            await dispatcher.stop()

            assert dispatcher._initialize_job_cache.await_count == seeded, leader_lease

    @pytest.mark.asyncio
    async def test_winning_the_lease_reseeds_the_cache_once(self):
        dispatcher = _dispatcher(IDENTITY)
        dispatcher._initialize_job_cache = AsyncMock()

        with patch(
            "gco.services.webhook_dispatcher.try_acquire_lease", return_value=True
        ) as acquire:
            assert await dispatcher._acquire_leadership() is True
            assert await dispatcher._acquire_leadership() is True

        acquire.assert_called_with(dispatcher.coordination_v1, IDENTITY, label="webhook")
        # Seeded on acquisition, not on every renewal.
        dispatcher._initialize_job_cache.assert_awaited_once()
        assert dispatcher.get_metrics()["leader"] is True

    @pytest.mark.asyncio
    async def test_losing_the_lease_stands_by_and_reseeds_on_the_next_win(self, caplog):
        dispatcher = _dispatcher(IDENTITY)
        dispatcher._initialize_job_cache = AsyncMock()

        with (
            patch(
                "gco.services.webhook_dispatcher.try_acquire_lease",
                side_effect=[True, False, False, True],
            ),
            caplog.at_level("INFO"),
        ):
            assert await dispatcher._acquire_leadership() is True
            assert await dispatcher._acquire_leadership() is False
            assert dispatcher.get_metrics()["leader"] is False
            assert await dispatcher._acquire_leadership() is False
            assert await dispatcher._acquire_leadership() is True

        assert "lost the deliverer Lease" in caplog.text
        assert dispatcher._initialize_job_cache.await_count == 2

    @pytest.mark.asyncio
    async def test_standby_never_watches_or_delivers(self):
        """A replica that does not hold the Lease must not open the job watch."""
        dispatcher = _dispatcher(IDENTITY)
        dispatcher._sync_watch_jobs = MagicMock(return_value=[])
        polls = 0

        async def stop_after_two_polls(_seconds):
            nonlocal polls
            polls += 1
            if polls == 2:
                dispatcher._running = False

        with (
            patch("gco.services.webhook_dispatcher.try_acquire_lease", return_value=False),
            patch(
                "gco.services.webhook_dispatcher.asyncio.sleep", side_effect=stop_after_two_polls
            ),
        ):
            dispatcher._running = True
            await dispatcher._watch_jobs()

        dispatcher._sync_watch_jobs.assert_not_called()
        assert polls == 2

    @pytest.mark.asyncio
    async def test_leader_watches_and_processes_events(self):
        dispatcher = _dispatcher(IDENTITY)
        dispatcher._initialize_job_cache = AsyncMock()
        job = MagicMock()
        dispatcher._sync_watch_jobs = MagicMock(return_value=[("MODIFIED", job)])
        dispatcher._process_job_event = AsyncMock()

        async def process_and_stop(event_type, event_job):
            dispatcher._running = False

        dispatcher._process_job_event.side_effect = process_and_stop

        with patch("gco.services.webhook_dispatcher.try_acquire_lease", return_value=True):
            dispatcher._running = True
            await dispatcher._watch_jobs()

        dispatcher._process_job_event.assert_awaited_once_with("MODIFIED", job)


class TestDispatcherFactory:
    def test_env_builds_the_lease_identity_from_pod_metadata(self, monkeypatch):
        from gco.services.webhook_dispatcher import create_webhook_dispatcher_from_env

        monkeypatch.setenv("POD_NAME", "health-monitor-7d9f")
        monkeypatch.setenv("POD_NAMESPACE", "gco-system")
        monkeypatch.setenv("WEBHOOK_LEASE_DURATION", "120")
        monkeypatch.delenv("WEBHOOK_LEASE_NAME", raising=False)
        with (
            patch("gco.services.webhook_dispatcher.config.load_incluster_config"),
            patch("gco.services.webhook_dispatcher.client.BatchV1Api"),
            patch("gco.services.webhook_dispatcher.client.CoordinationV1Api"),
            patch("gco.services.webhook_dispatcher.get_webhook_store"),
        ):
            dispatcher = create_webhook_dispatcher_from_env()

        assert dispatcher._leader_lease == LeaseIdentity(
            name="gco-health-monitor-webhooks",
            namespace="gco-system",
            holder="health-monitor-7d9f",
            duration_seconds=120,
        )

    def test_empty_lease_name_opts_out_of_election(self, monkeypatch):
        from gco.services.webhook_dispatcher import create_webhook_dispatcher_from_env

        monkeypatch.setenv("WEBHOOK_LEASE_NAME", "  ")
        with (
            patch("gco.services.webhook_dispatcher.config.load_incluster_config"),
            patch("gco.services.webhook_dispatcher.client.BatchV1Api"),
            patch("gco.services.webhook_dispatcher.client.CoordinationV1Api"),
            patch("gco.services.webhook_dispatcher.get_webhook_store"),
        ):
            dispatcher = create_webhook_dispatcher_from_env()

        assert dispatcher._leader_lease is None

    def test_holder_falls_back_to_hostname_then_pid(self, monkeypatch):
        from gco.services.webhook_dispatcher import create_webhook_dispatcher_from_env

        monkeypatch.delenv("POD_NAME", raising=False)
        monkeypatch.delenv("WEBHOOK_LEASE_NAME", raising=False)
        monkeypatch.delenv("WEBHOOK_LEASE_DURATION", raising=False)
        monkeypatch.setenv("HOSTNAME", "hm-host")
        with (
            patch("gco.services.webhook_dispatcher.config.load_incluster_config"),
            patch("gco.services.webhook_dispatcher.client.BatchV1Api"),
            patch("gco.services.webhook_dispatcher.client.CoordinationV1Api"),
            patch("gco.services.webhook_dispatcher.get_webhook_store"),
        ):
            first = create_webhook_dispatcher_from_env()
            monkeypatch.delenv("HOSTNAME", raising=False)
            second = create_webhook_dispatcher_from_env()

        assert first._leader_lease is not None and first._leader_lease.holder == "hm-host"
        assert first._leader_lease.duration_seconds == 90
        assert second._leader_lease is not None
        assert second._leader_lease.holder.startswith("webhook-dispatcher-")


def test_health_monitor_alb_sync_uses_the_shared_election():
    """The ALB-sync Lease keeps its behaviour through the shared implementation."""
    from gco.models import ResourceThresholds
    from gco.services.health_monitor import HealthMonitor

    with (
        patch("gco.services.health_monitor.config.load_incluster_config"),
        patch("gco.services.health_monitor.client"),
        patch("gco.services.health_monitor.try_acquire_lease", return_value=True) as acquire,
    ):
        monitor = HealthMonitor(
            cluster_id="c", region="us-east-1", thresholds=ResourceThresholds(80, 80, -1)
        )
        assert monitor._try_acquire_alb_sync_lease() is True

    acquire.assert_called_once()
    args, kwargs = acquire.call_args
    assert args[0] is monitor.coordination_v1
    assert args[1] == LeaseIdentity(
        name="gco-health-monitor-alb-sync",
        namespace="gco-system",
        holder=monitor._alb_sync_holder,
        duration_seconds=90,
    )
    assert kwargs == {"label": "ALB-sync", "request_timeout": LEASE_REQUEST_TIMEOUT}
