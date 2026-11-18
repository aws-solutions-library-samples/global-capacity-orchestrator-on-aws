"""List-then-watch contract of the webhook dispatcher's Job watcher.

Drives ``WebhookDispatcher`` against an in-process stand-in for the API
server's Job list and watch endpoints, read through the real
``kubernetes.watch.Watch``. A list answers with its resourceVersion; a watch
from a version streams only the changes after it, ends with a bookmark, and
answers 410 Gone for a compacted version; a watch without a version replays
every Job as ADDED, which is what the old watcher asked for on every cycle.

Pins that the watcher resumes from the version it processed instead of
replaying, carries server-side and client timeouts, lists again on 410 Gone
and fires what changed in the gap, never skips events after a mid-stream
error, delivers nothing until a list has succeeded, backs off exponentially
on errors, and that a re-seed replaces the job-state cache instead of
merging into it.
"""

from __future__ import annotations

import itertools
import json
from collections.abc import Iterator
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from kubernetes import config as k8s_config
from kubernetes.client import ApiClient
from kubernetes.client.exceptions import ApiException
from kubernetes.client.models import V1JobList
from urllib3.exceptions import ProtocolError

from gco.services import webhook_dispatcher as wd
from gco.services.leader_lease import LEASE_MIN_DURATION_SECONDS, LeaseIdentity, LeaseOutcome

_STATUS = {
    "running": {"active": 1},
    "succeeded": {"succeeded": 1, "conditions": [{"type": "Complete", "status": "True"}]},
}


def _raw_job(name: str, resource_version: int, state: str, namespace: str) -> dict[str, Any]:
    return {
        "apiVersion": "batch/v1",
        "kind": "Job",
        "metadata": {
            "name": name,
            "namespace": namespace,
            "uid": f"uid-{name}",
            "resourceVersion": str(resource_version),
        },
        "spec": {"template": {"spec": {"containers": []}}},
        "status": dict(_STATUS[state]),
    }


class _WatchResponse:
    """The streamed body of one watch request, one JSON event per line."""

    status = 200

    def __init__(self, events: list[dict[str, Any]], fail_after: int | None) -> None:
        self._lines = [json.dumps(event).encode() + b"\n" for event in events]
        self._fail_after = fail_after

    def stream(self, amt: Any = None, decode_content: bool = False) -> Iterator[bytes]:
        for index, line in enumerate(self._lines):
            if self._fail_after is not None and index >= self._fail_after:
                raise ProtocolError("Connection broken: connection reset by peer")
            yield line

    def close(self) -> None:
        pass

    def release_conn(self) -> None:
        pass


class FakeJobApi:
    """Job list and watch endpoints with API-server version semantics."""

    def __init__(self) -> None:
        self.resource_version = 100
        self.jobs: dict[str, dict[str, Any]] = {}
        self.history: list[tuple[int, str, dict[str, Any]]] = []
        self.compacted_before = 0
        self.lists = 0
        self.list_error: Exception | None = None
        self.watches: list[dict[str, Any]] = []
        self.watch_errors: list[Exception | None] = []
        self.fail_watch_after: int | None = None
        # A watcher that replays instead of resuming never sleeps between
        # requests; failing past this budget sends it to its error path so a
        # regression ends the test instead of spinning forever.
        self.watch_budget = 25

    def put(self, name: str, state: str = "running", namespace: str = "gco-jobs") -> None:
        self.resource_version += 1
        event_type = "MODIFIED" if name in self.jobs else "ADDED"
        self.jobs[name] = _raw_job(name, self.resource_version, state, namespace)
        self.history.append((self.resource_version, event_type, self.jobs[name]))

    def delete(self, name: str) -> None:
        self.resource_version += 1
        deleted = json.loads(json.dumps(self.jobs.pop(name)))
        deleted["metadata"]["resourceVersion"] = str(self.resource_version)
        self.history.append((self.resource_version, "DELETED", deleted))

    def compact(self) -> None:
        """Forget history: a watch from any earlier version is now too old."""
        self.compacted_before = self.resource_version

    def list_job_for_all_namespaces(self, **kwargs: Any) -> V1JobList:
        if not kwargs.get("watch"):
            self.lists += 1
            if self.list_error is not None:
                raise self.list_error
            body = {
                "apiVersion": "batch/v1",
                "kind": "JobList",
                "metadata": {"resourceVersion": str(self.resource_version)},
                "items": [self.jobs[name] for name in sorted(self.jobs)],
            }
            return ApiClient().deserialize(json.dumps(body), "V1JobList", "application/json")

        self.watches.append(kwargs)
        if len(self.watches) > self.watch_budget:
            raise RuntimeError("watch budget exceeded")
        if self.watch_errors:
            error = self.watch_errors.pop(0)
            if error is not None:
                raise error
        since = kwargs.get("resource_version")
        events: list[dict[str, Any]]
        if since is None:
            # "Get state and start at most recent": every Job replayed as ADDED.
            events = [{"type": "ADDED", "object": self.jobs[name]} for name in sorted(self.jobs)]
        elif int(since) < self.compacted_before:
            events = [
                {
                    "type": "ERROR",
                    "object": {
                        "kind": "Status",
                        "apiVersion": "v1",
                        "status": "Failure",
                        "message": f"too old resource version: {since}",
                        "reason": "Expired",
                        "code": 410,
                    },
                }
            ]
        else:
            events = [
                {"type": event_type, "object": raw}
                for version, event_type, raw in self.history
                if version > int(since)
            ]
            if kwargs.get("allow_watch_bookmarks"):
                events.append(
                    {
                        "type": "BOOKMARK",
                        "object": {
                            "kind": "Job",
                            "apiVersion": "batch/v1",
                            "metadata": {"resourceVersion": str(self.resource_version)},
                        },
                    }
                )
        return _WatchResponse(events, self.fail_watch_after)  # type: ignore[return-value]


def _dispatcher(
    api: FakeJobApi, **kwargs: Any
) -> tuple[wd.WebhookDispatcher, list[tuple[str, str]]]:
    with patch.object(wd, "config") as cfg, patch.object(wd, "client"):
        cfg.ConfigException = k8s_config.ConfigException
        cfg.load_incluster_config.return_value = None
        dispatcher = wd.WebhookDispatcher(
            "c", "r", webhook_store=MagicMock(), namespaces=["gco-jobs"], **kwargs
        )
    dispatcher.batch_v1 = api
    fired: list[tuple[str, str]] = []

    async def record(event: wd.WebhookEvent, job: Any) -> list[wd.WebhookDeliveryResult]:
        fired.append((event.value, job.metadata.name))
        return []

    dispatcher._dispatch_event = record  # type: ignore[method-assign]
    dispatcher._running = True
    return dispatcher, fired


async def _watch_cycle(dispatcher: wd.WebhookDispatcher) -> list[tuple[str, Any]]:
    events = dispatcher._sync_watch_jobs()
    await dispatcher._process_job_events(events)
    return events


class TestResumeFromResourceVersion:
    @pytest.mark.asyncio
    async def test_watch_resumes_from_the_listed_version_instead_of_replaying(self):
        api = FakeJobApi()
        for index in range(12):
            api.put(f"job-{index:02d}")
        dispatcher, fired = _dispatcher(api)

        assert await dispatcher._initialize_job_cache() is True
        listed_version = str(api.resource_version)
        api.put("job-11", state="succeeded")
        for _ in range(4):
            await _watch_cycle(dispatcher)

        assert fired == [("job.completed", "job-11")]
        latest = str(api.resource_version)
        assert [request["resource_version"] for request in api.watches] == [
            listed_version,
            latest,
            latest,
            latest,
        ]

    def test_each_watch_request_is_bounded_on_both_ends(self):
        api = FakeJobApi()
        dispatcher, _ = _dispatcher(api)
        dispatcher._resource_version = "100"

        dispatcher._sync_watch_jobs()

        (request,) = api.watches
        assert request["timeout_seconds"] == wd.WATCH_TIMEOUT_SECONDS
        assert request["allow_watch_bookmarks"] is True
        connect, read = request["_request_timeout"]
        assert connect == dispatcher._k8s_timeout
        assert read == wd.WATCH_TIMEOUT_SECONDS + wd.WATCH_READ_GRACE_SECONDS
        assert read > request["timeout_seconds"]
        # The loop re-checks the deliverer Lease between watch requests.
        assert read < LEASE_MIN_DURATION_SECONDS

    @pytest.mark.asyncio
    async def test_a_full_batch_resumes_after_its_last_event(self):
        api = FakeJobApi()
        dispatcher, fired = _dispatcher(api)
        assert await dispatcher._initialize_job_cache() is True
        for index in range(15):
            api.put(f"burst-{index:02d}")

        first = await _watch_cycle(dispatcher)
        second = await _watch_cycle(dispatcher)

        assert len(first) == wd.WATCH_BATCH_LIMIT
        assert len(second) == 15 - wd.WATCH_BATCH_LIMIT
        assert fired == [("job.started", f"burst-{index:02d}") for index in range(15)]

    @pytest.mark.asyncio
    async def test_bookmarks_advance_the_version_without_events(self):
        api = FakeJobApi()
        api.put("idle")
        dispatcher, fired = _dispatcher(api)
        assert await dispatcher._initialize_job_cache() is True
        api.resource_version += 40  # other kinds changed; no Job did

        assert await _watch_cycle(dispatcher) == []

        assert dispatcher._resource_version == str(api.resource_version)
        assert fired == []

    @pytest.mark.asyncio
    async def test_an_error_mid_stream_does_not_skip_events(self):
        api = FakeJobApi()
        dispatcher, fired = _dispatcher(api)
        assert await dispatcher._initialize_job_cache() is True
        listed_version = dispatcher._resource_version
        api.put("first")
        api.put("second")
        api.fail_watch_after = 1

        with pytest.raises(ProtocolError):
            dispatcher._sync_watch_jobs()
        assert dispatcher._resource_version == listed_version

        api.fail_watch_after = None
        await _watch_cycle(dispatcher)
        assert fired == [("job.started", "first"), ("job.started", "second")]

    def test_nothing_is_watched_before_a_list(self):
        api = FakeJobApi()
        dispatcher, _ = _dispatcher(api)

        assert dispatcher._sync_watch_jobs() == []
        assert api.watches == []


class TestExpiredResourceVersion:
    @pytest.mark.asyncio
    async def test_410_lists_again_and_fires_what_changed_in_the_gap(self):
        api = FakeJobApi()
        for name in ("a", "b", "c"):
            api.put(name)
        dispatcher, fired = _dispatcher(api)
        assert await dispatcher._initialize_job_cache() is True
        api.put("a", state="succeeded")
        api.delete("b")
        api.put("d")
        api.compact()

        assert dispatcher._sync_watch_jobs() == []
        assert dispatcher._resource_version is None

        assert await dispatcher._initialize_job_cache(deliver_changes=True) is True
        assert fired == [("job.completed", "a"), ("job.started", "d")]
        assert sorted(dispatcher._job_state_cache.job_states) == ["uid-a", "uid-c", "uid-d"]
        assert dispatcher._resource_version == str(api.resource_version)

    @pytest.mark.asyncio
    async def test_the_loop_relists_after_410_without_reseeding_silently(self):
        api = FakeJobApi()
        api.put("a")
        dispatcher, fired = _dispatcher(api)
        assert await dispatcher._initialize_job_cache() is True
        api.put("a", state="succeeded")
        api.compact()
        sleeps: list[float] = []

        async def fake_sleep(seconds: float) -> None:
            sleeps.append(seconds)
            if len(sleeps) == 2:
                dispatcher._running = False

        with patch.object(wd.asyncio, "sleep", fake_sleep):
            await dispatcher._watch_jobs()

        assert fired == [("job.completed", "a")]
        assert api.lists == 2
        assert [request["resource_version"] for request in api.watches] == [
            "101",
            str(api.resource_version),
        ]


class TestNoDeliveryBeforeASeed:
    @pytest.mark.asyncio
    async def test_failed_lists_deliver_nothing_and_back_off(self):
        api = FakeJobApi()
        api.put("a")
        api.put("b")
        dispatcher, fired = _dispatcher(api, leader_lease=LeaseIdentity("l", "ns", "replica-b", 90))
        api.list_error = TimeoutError("read timed out")
        sleeps: list[float] = []

        async def fake_sleep(seconds: float) -> None:
            sleeps.append(seconds)
            if len(sleeps) == 3:
                api.list_error = None
            if len(sleeps) == 5:
                dispatcher._running = False

        assert dispatcher._leader_elector is not None
        outcomes = itertools.chain([LeaseOutcome.ACQUIRED], itertools.repeat(LeaseOutcome.RENEWED))
        with (
            patch.object(dispatcher._leader_elector, "try_acquire", side_effect=outcomes),
            patch.object(wd.asyncio, "sleep", fake_sleep),
        ):
            await dispatcher._watch_jobs()

        assert fired == []
        assert sleeps[:3] == [1.0, 2.0, 4.0]
        assert api.lists == 4
        assert len(api.watches) == 2
        assert all(request["resource_version"] == "102" for request in api.watches)

    @pytest.mark.asyncio
    async def test_a_failed_seed_reports_failure_and_keeps_the_watch_closed(self):
        api = FakeJobApi()
        api.list_error = ApiException(status=500)
        dispatcher, _ = _dispatcher(api)

        assert await dispatcher._initialize_job_cache() is False
        assert dispatcher._resource_version is None
        assert dispatcher._sync_watch_jobs() == []

    @pytest.mark.asyncio
    async def test_a_list_without_a_resource_version_is_a_failed_seed(self):
        dispatcher, _ = _dispatcher(FakeJobApi())
        listing = MagicMock(items=[])
        listing.metadata.resource_version = None
        dispatcher.batch_v1 = MagicMock()
        dispatcher.batch_v1.list_job_for_all_namespaces.return_value = listing

        assert await dispatcher._initialize_job_cache() is False
        assert dispatcher._resource_version is None


class TestBackoff:
    @pytest.mark.asyncio
    async def test_watch_errors_back_off_exponentially_and_reset_after_success(self):
        api = FakeJobApi()
        boom = ApiException(status=500, reason="Internal error")
        api.watch_errors = [boom, boom, boom, None, boom]
        dispatcher, _ = _dispatcher(api)
        sleeps: list[float] = []

        async def fake_sleep(seconds: float) -> None:
            sleeps.append(seconds)
            if len(sleeps) == 5:
                dispatcher._running = False

        with patch.object(wd.asyncio, "sleep", fake_sleep):
            await dispatcher._watch_jobs()

        # Three failures double the delay; a clean (idle) watch pauses one
        # second and resets the count, so the next failure starts over.
        assert sleeps == [1.0, 2.0, 4.0, 1, 1.0]
        assert api.lists == 1

    @pytest.mark.parametrize(
        ("failures", "delay"),
        [(1, 1.0), (2, 2.0), (6, 32.0), (7, 60.0), (10_000, 60.0)],
    )
    def test_backoff_doubles_up_to_the_cap(self, failures: int, delay: float):
        assert wd._retry_backoff_seconds(failures) == delay


class TestReseedReplacesTheCache:
    @pytest.mark.asyncio
    async def test_jobs_deleted_before_a_reseed_leave_the_cache(self):
        api = FakeJobApi()
        api.put("old")
        api.put("other", namespace="elsewhere")
        dispatcher, fired = _dispatcher(api)
        assert await dispatcher._initialize_job_cache() is True
        api.delete("old")
        api.put("new")

        assert await dispatcher._initialize_job_cache() is True

        assert sorted(dispatcher._job_state_cache.job_states) == ["uid-new"]
        assert fired == []
