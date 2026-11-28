"""Single-writer Lease election shared by the health-monitor's elected duties.

``gco.services.leader_lease`` is the one implementation behind the ALB→SSM
sync and the webhook deliverer election, so its rules are pinned here once:
optimistic replace, a foreign holder expires only after this replica has seen
its record unchanged for the lease duration on its own monotonic clock (never
by comparing ``renewTime`` with the local wall clock), a timestamp-less holder
may be replaced at once, a racing writer (HTTP 409) loses the cycle, every
failure fails closed, and each attempt reports whether it acquired or renewed.
That expiry rule is ``LeaseObserver``, which the inference monitor's own
election applies too.
The second half covers how the webhook dispatcher consumes it — standbys
never deliver, a new leader re-seeds its cache before delivering (including
after a loss it never observed), and a dispatcher built without a Lease keeps
the single-replica behaviour.
"""

from __future__ import annotations

import copy
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from kubernetes.client.models import V1Lease, V1LeaseSpec, V1ObjectMeta
from kubernetes.client.rest import ApiException

from gco.services.leader_lease import (
    LEASE_MIN_DURATION_SECONDS,
    LEASE_REQUEST_TIMEOUT,
    LeaseElector,
    LeaseIdentity,
    LeaseObserver,
    LeaseOutcome,
    lease_duration_from_env,
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
    lease.spec.acquire_time = None
    return lease


def _api(lease: MagicMock) -> MagicMock:
    api = MagicMock()
    api.read_namespaced_lease.return_value = lease
    return api


class FakeClock:
    """A monotonic clock the test moves by hand."""

    def __init__(self) -> None:
        self.now = 1_000.0

    def __call__(self) -> float:
        return self.now


def _elector(label: str = "webhook", clock: FakeClock | None = None) -> LeaseElector:
    return LeaseElector(IDENTITY, label=label, clock=clock or FakeClock())


class FakeCoordination:
    """A Lease endpoint with optimistic concurrency on resourceVersion."""

    def __init__(self) -> None:
        self.lease = V1Lease(
            metadata=V1ObjectMeta(
                name=IDENTITY.name, namespace=IDENTITY.namespace, resource_version="1"
            ),
            spec=V1LeaseSpec(),
        )

    def read_namespaced_lease(self, name, namespace, _request_timeout=None):
        return copy.deepcopy(self.lease)

    def replace_namespaced_lease(self, name, namespace, body, _request_timeout=None):
        if body.metadata.resource_version != self.lease.metadata.resource_version:
            raise ApiException(status=409)
        body.metadata.resource_version = str(int(self.lease.metadata.resource_version) + 1)
        self.lease = copy.deepcopy(body)
        return body

    def write_as(self, holder: str | None) -> None:
        """Another replica rewrites the Lease (an acquisition, renewal, or release)."""
        spec = self.lease.spec
        if spec.holder_identity != holder:
            spec.lease_transitions = (spec.lease_transitions or 0) + 1
        spec.holder_identity = holder
        spec.renew_time = datetime.now(UTC)
        self.lease.metadata.resource_version = str(int(self.lease.metadata.resource_version) + 1)


class TestLeaseElector:
    def test_unheld_lease_is_acquired_with_a_transition(self):
        lease = _lease(None, None)
        api = _api(lease)

        assert _elector().try_acquire(api) is LeaseOutcome.ACQUIRED

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

        assert _elector().try_acquire(api) is LeaseOutcome.RENEWED

        assert lease.spec.lease_transitions == 2
        assert lease.spec.acquire_time == acquired_at
        assert lease.spec.renew_time > acquired_at

    def test_outcomes_say_whether_the_lease_is_held(self):
        assert LeaseOutcome.ACQUIRED.held is True
        assert LeaseOutcome.RENEWED.held is True
        assert LeaseOutcome.NOT_HELD.held is False

    def test_live_foreign_holder_blocks_without_writing(self):
        api = _api(_lease("other-replica", datetime.now(UTC)))

        assert _elector().try_acquire(api) is LeaseOutcome.NOT_HELD

        api.replace_namespaced_lease.assert_not_called()

    def test_a_foreign_holder_is_judged_by_our_clock_not_its_renew_time(self):
        """A renewTime far behind our wall clock is not proof the holder is gone."""
        clock = FakeClock()
        elector = _elector(clock=clock)
        # Our wall clock runs ten minutes ahead of the holder's.
        lease = _lease("other-replica", datetime.now(UTC) - timedelta(minutes=10))
        api = _api(lease)

        assert elector.try_acquire(api) is LeaseOutcome.NOT_HELD
        clock.now += 89
        assert elector.try_acquire(api) is LeaseOutcome.NOT_HELD
        api.replace_namespaced_lease.assert_not_called()

        clock.now += 1  # unchanged for a full lease duration on our clock
        assert elector.try_acquire(api) is LeaseOutcome.ACQUIRED
        assert lease.spec.holder_identity == IDENTITY.holder
        assert lease.spec.lease_transitions == 3

    def test_a_holder_that_keeps_renewing_is_never_replaced(self):
        clock = FakeClock()
        elector = _elector(clock=clock)
        api = MagicMock()
        for step in range(5):
            # A stale-looking renewTime that still changes every read.
            renewed = datetime.now(UTC) - timedelta(hours=1) + timedelta(seconds=step)
            api.read_namespaced_lease.return_value = _lease("other-replica", renewed)
            assert elector.try_acquire(api) is LeaseOutcome.NOT_HELD
            clock.now += 200

        api.replace_namespaced_lease.assert_not_called()

    def test_holder_without_renew_time_is_treated_as_expired(self):
        lease = _lease("wedged-replica", None)
        api = _api(lease)

        assert _elector().try_acquire(api) is LeaseOutcome.ACQUIRED
        assert lease.spec.holder_identity == IDENTITY.holder

    def test_missing_lease_duration_falls_back_to_the_identity(self):
        clock = FakeClock()
        elector = _elector(clock=clock)
        lease = _lease("other-replica", datetime.now(UTC), duration=None)
        api = _api(lease)

        assert elector.try_acquire(api) is LeaseOutcome.NOT_HELD
        clock.now += IDENTITY.duration_seconds - 1
        assert elector.try_acquire(api) is LeaseOutcome.NOT_HELD
        clock.now += 1
        assert elector.try_acquire(api) is LeaseOutcome.ACQUIRED

    def test_regaining_after_another_holder_reports_an_acquisition(self):
        """A hold this replica never saw end is reported as a new acquisition."""
        coordination = FakeCoordination()
        clock = FakeClock()
        elector = _elector(clock=clock)

        assert elector.try_acquire(coordination) is LeaseOutcome.ACQUIRED
        assert elector.try_acquire(coordination) is LeaseOutcome.RENEWED
        # While this replica stalls, another one takes the Lease and releases it.
        coordination.write_as("replica-b")
        coordination.write_as("")

        assert elector.try_acquire(coordination) is LeaseOutcome.ACQUIRED
        assert coordination.lease.spec.holder_identity == IDENTITY.holder

    def test_lost_race_returns_not_held(self, caplog):
        api = _api(_lease(None, None))
        api.replace_namespaced_lease.side_effect = ApiException(status=409)

        with caplog.at_level("DEBUG", logger="gco.services.leader_lease"):
            assert _elector().try_acquire(api) is LeaseOutcome.NOT_HELD
        assert "Lost webhook Lease race" in caplog.text

    def test_replace_failure_other_than_conflict_fails_closed(self, caplog):
        api = _api(_lease(None, None))
        api.replace_namespaced_lease.side_effect = ApiException(status=500)

        with caplog.at_level("WARNING"):
            assert _elector().try_acquire(api) is LeaseOutcome.NOT_HELD
        assert "webhook Lease check failed" in caplog.text

    def test_missing_precreated_lease_disables_the_duty(self, caplog):
        api = MagicMock()
        api.read_namespaced_lease.side_effect = ApiException(status=404)

        with caplog.at_level("WARNING"):
            assert _elector().try_acquire(api) is LeaseOutcome.NOT_HELD

        assert "gco-system/gco-health-monitor-webhooks is missing" in caplog.text
        api.create_namespaced_lease.assert_not_called()

    def test_unexpected_error_fails_closed(self, caplog):
        api = MagicMock()
        api.read_namespaced_lease.side_effect = RuntimeError("socket closed")

        with caplog.at_level("WARNING"):
            assert _elector(label="ALB-sync").try_acquire(api) is LeaseOutcome.NOT_HELD
        assert "ALB-sync Lease check failed (non-fatal): socket closed" in caplog.text


class TestLeaseObserver:
    """The expiry rule for a hold this replica does not own."""

    def test_an_unchanged_record_expires_after_the_duration(self):
        clock = FakeClock()
        observer = LeaseObserver(clock)
        # A renewTime an hour behind our wall clock changes nothing.
        spec = _lease("other-replica", datetime.now(UTC) - timedelta(hours=1)).spec

        assert observer.expired(spec, 90) is False
        clock.now += 89
        assert observer.expired(spec, 90) is False
        clock.now += 1
        assert observer.expired(spec, 90) is True

    def test_a_renewal_restarts_the_timer(self):
        clock = FakeClock()
        observer = LeaseObserver(clock)
        spec = _lease("other-replica", datetime.now(UTC)).spec
        observer.observe(spec)
        clock.now += 90
        spec.renew_time += timedelta(seconds=1)

        assert observer.expired(spec, 90) is False
        clock.now += 90
        assert observer.expired(spec, 90) is True

    @pytest.mark.parametrize(
        "field", ["holder_identity", "acquire_time", "lease_transitions", "lease_duration_seconds"]
    )
    def test_any_record_field_counts_as_a_change(self, field):
        clock = FakeClock()
        observer = LeaseObserver(clock)
        spec = _lease("other-replica", datetime.now(UTC)).spec
        observer.observe(spec)
        clock.now += 90
        setattr(spec, field, "rewritten")

        assert observer.expired(spec, 90) is False

    def test_a_record_without_renew_time_has_expired_already(self):
        observer = LeaseObserver(FakeClock())

        assert observer.expired(_lease("wedged-replica", None).spec, 90) is True


def test_the_elector_and_the_inference_monitor_share_one_expiry_rule():
    """Neither election keeps a copy of the rule: both defer to LeaseObserver."""
    from gco.services.inference_monitor import InferenceMonitor

    with (
        patch("gco.services.inference_monitor.config.load_incluster_config"),
        patch("gco.services.inference_monitor.client.AppsV1Api"),
        patch("gco.services.inference_monitor.client.CoreV1Api"),
        patch("gco.services.inference_monitor.client.NetworkingV1Api"),
        patch("gco.services.inference_monitor.client.DiscoveryV1Api"),
    ):
        monitor = InferenceMonitor("cluster", "us-east-1", MagicMock())
    # A foreign record renewed just now and seen for the first time: live by
    # the rule, so each election taking it proves it asked the observer.
    renewed = datetime.now(UTC)

    with (
        patch.object(LeaseObserver, "expired", return_value=True) as expired,
        patch("gco.services.inference_monitor.client.CoordinationV1Api") as coordination,
    ):
        coordination.return_value.read_namespaced_lease.return_value = _lease("other", renewed)
        assert _elector().try_acquire(_api(_lease("other", renewed))) is LeaseOutcome.ACQUIRED
        assert monitor._try_acquire_lease("inference-monitor-leader", "monitor-a") is True

    assert expired.call_count == 2


class TestLeaseRelease:
    def test_the_holder_releases_and_a_standby_takes_over_at_once(self):
        coordination = FakeCoordination()
        clock = FakeClock()
        leader = _elector(clock=clock)
        standby = LeaseElector(
            LeaseIdentity(IDENTITY.name, IDENTITY.namespace, "health-monitor-xyz", 90),
            label="webhook",
            clock=clock,
        )
        assert leader.try_acquire(coordination) is LeaseOutcome.ACQUIRED
        assert standby.try_acquire(coordination) is LeaseOutcome.NOT_HELD

        assert leader.release(coordination) is True

        assert coordination.lease.spec.holder_identity is None
        # No lease duration to wait out: the standby wins on its next attempt.
        assert standby.try_acquire(coordination) is LeaseOutcome.ACQUIRED

    def test_another_replicas_hold_is_never_released(self):
        lease = _lease("other-replica", datetime.now(UTC))
        api = _api(lease)

        assert _elector().release(api) is False

        api.replace_namespaced_lease.assert_not_called()
        assert lease.spec.holder_identity == "other-replica"

    @pytest.mark.parametrize("failing_call", ["read_namespaced_lease", "replace_namespaced_lease"])
    def test_a_failed_release_is_non_fatal(self, failing_call, caplog):
        api = _api(_lease(IDENTITY.holder, datetime.now(UTC)))
        getattr(api, failing_call).side_effect = ApiException(status=409)

        with caplog.at_level("WARNING"):
            assert _elector().release(api) is False
        assert "Could not release the webhook Lease" in caplog.text


class TestDispatcherReleasesOnStop:
    @pytest.mark.asyncio
    async def test_a_deliverer_releases_the_lease_when_it_stops(self):
        dispatcher = _dispatcher(IDENTITY)
        coordination = FakeCoordination()
        dispatcher.coordination_v1 = coordination
        dispatcher._watch_jobs = AsyncMock()
        assert await dispatcher._acquire_leadership() is True

        await dispatcher.start()
        await dispatcher.stop()

        assert coordination.lease.spec.holder_identity is None
        assert dispatcher.get_metrics()["leader"] is False

    @pytest.mark.asyncio
    async def test_a_standby_or_lease_free_dispatcher_releases_nothing(self):
        for leader_lease in (IDENTITY, None):
            dispatcher = _dispatcher(leader_lease)
            dispatcher._initialize_job_cache = AsyncMock(return_value=True)
            dispatcher._watch_jobs = AsyncMock()
            await dispatcher.start()
            with patch.object(LeaseElector, "release") as release:
                await dispatcher.stop()
            release.assert_not_called()


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
    async def test_winning_the_lease_discards_the_cache_once(self):
        dispatcher = _dispatcher(IDENTITY)
        dispatcher._resource_version = "5"
        dispatcher._cache_seeded = True

        with patch.object(
            dispatcher._leader_elector,
            "try_acquire",
            side_effect=[LeaseOutcome.ACQUIRED, LeaseOutcome.RENEWED],
        ) as acquire:
            assert await dispatcher._acquire_leadership() is True
            # Won: the watcher must list (silently) before it delivers.
            assert dispatcher._resource_version is None
            assert dispatcher._cache_seeded is False
            dispatcher._resource_version = "6"  # the watcher's seed
            dispatcher._cache_seeded = True
            assert await dispatcher._acquire_leadership() is True

        acquire.assert_called_with(dispatcher.coordination_v1)
        # A renewal keeps the cache and the resume point.
        assert dispatcher._resource_version == "6"
        assert dispatcher._cache_seeded is True
        assert dispatcher.get_metrics()["leader"] is True

    @pytest.mark.asyncio
    async def test_losing_the_lease_stands_by_and_reseeds_on_the_next_win(self, caplog):
        dispatcher = _dispatcher(IDENTITY)

        with (
            patch.object(
                dispatcher._leader_elector,
                "try_acquire",
                side_effect=[
                    LeaseOutcome.ACQUIRED,
                    LeaseOutcome.NOT_HELD,
                    LeaseOutcome.NOT_HELD,
                    LeaseOutcome.ACQUIRED,
                ],
            ),
            caplog.at_level("INFO"),
        ):
            assert await dispatcher._acquire_leadership() is True
            dispatcher._resource_version = "6"  # the watcher's seed
            dispatcher._cache_seeded = True
            assert await dispatcher._acquire_leadership() is False
            assert dispatcher.get_metrics()["leader"] is False
            assert await dispatcher._acquire_leadership() is False
            assert await dispatcher._acquire_leadership() is True

        assert "lost the deliverer Lease" in caplog.text
        assert dispatcher._resource_version is None
        assert dispatcher._cache_seeded is False

    @pytest.mark.asyncio
    async def test_a_lease_lost_and_regained_unseen_reseeds_the_cache(self):
        """Another replica held (and delivered) in between: never a renewal."""
        dispatcher = _dispatcher(IDENTITY)
        coordination = FakeCoordination()
        dispatcher.coordination_v1 = coordination

        assert await dispatcher._acquire_leadership() is True
        dispatcher._resource_version = "6"  # the watcher's seed
        dispatcher._cache_seeded = True
        # This replica stalls past the lease; another takes over, delivers,
        # and releases the Lease on shutdown.
        coordination.write_as("replica-b")
        coordination.write_as("")

        assert await dispatcher._acquire_leadership() is True
        assert dispatcher._resource_version is None
        assert dispatcher._cache_seeded is False

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
            patch.object(
                dispatcher._leader_elector, "try_acquire", return_value=LeaseOutcome.NOT_HELD
            ),
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

        with patch.object(
            dispatcher._leader_elector, "try_acquire", return_value=LeaseOutcome.ACQUIRED
        ):
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
    ):
        monitor = HealthMonitor(
            cluster_id="c", region="us-east-1", thresholds=ResourceThresholds(80, 80, -1)
        )

    elector = monitor._alb_sync_elector
    assert elector.identity == LeaseIdentity(
        name="gco-health-monitor-alb-sync",
        namespace="gco-system",
        holder=monitor._alb_sync_holder,
        duration_seconds=90,
    )
    assert elector.label == "ALB-sync"
    assert elector.request_timeout == LEASE_REQUEST_TIMEOUT
    for outcome, held in (
        (LeaseOutcome.ACQUIRED, True),
        (LeaseOutcome.RENEWED, True),
        (LeaseOutcome.NOT_HELD, False),
    ):
        with patch.object(elector, "try_acquire", return_value=outcome) as acquire:
            assert monitor._try_acquire_alb_sync_lease() is held
        acquire.assert_called_once_with(monitor.coordination_v1)
