"""
Tests for scripts/test_webhook_delivery.py.

The script itself is a manual integration harness — it starts a local HTTP
server, dispatches a webhook through the real ``WebhookDispatcher``, and
prints the result. These unit tests cover the script's own helpers and the
argparse-driven dispatch in ``main()`` without spinning up a real dispatcher
or hitting the network.

Scope:
    - ``WebhookHandler.do_POST``       — captures headers and body into the
                                         module-level ``received_webhooks``
                                         list and returns 200 + JSON body.
    - ``WebhookHandler.log_message``   — silenced so recordings stay clean.
    - ``start_local_server``           — binds on the requested port, runs
                                         in a daemon thread, shuts down cleanly.
    - ``create_mock_job``              — fixture factory returning a MagicMock
                                         shaped like a completed K8s Job.
    - ``main``                         — argparse branch selection between
                                         local-server and external-URL modes;
                                         exit code propagates from the chosen
                                         async runner.

The script was already underscore-named so a direct import works. We rely on
``testpaths = ["tests"]`` in pytest config to stop pytest from collecting the
script itself as a test module.
"""

import asyncio
import json
import socket
import sys
import threading
import time
import urllib.request
from datetime import datetime
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

# Import the module under test.
sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))
import test_webhook_delivery as harness  # noqa: E402 - sys.path set above

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _free_port() -> int:
    """Grab an unused loopback port so tests don't collide on CI."""
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


@pytest.fixture(autouse=True)
def _clear_received_webhooks():
    """The harness stashes received webhooks in a module-level list.

    Reset before and after every test so assertions remain independent of
    test order.
    """
    harness.received_webhooks.clear()
    yield
    harness.received_webhooks.clear()


# ---------------------------------------------------------------------------
# WebhookHandler
# ---------------------------------------------------------------------------


class TestWebhookHandler:
    """The live HTTP handler should capture everything it receives."""

    def test_do_post_records_headers_body_and_responds_200(self):
        port = _free_port()
        server = harness.start_local_server(port)
        try:
            body = json.dumps({"event": "job.completed", "job": "test-1"}).encode()
            req = urllib.request.Request(
                url=f"http://127.0.0.1:{port}/webhook",
                data=body,
                headers={
                    "Content-Type": "application/json",
                    "X-GCO-Event": "job.completed",
                    "X-GCO-Cluster": "gco-us-east-1",
                    "X-GCO-Region": "us-east-1",
                    "X-GCO-Signature": "sha256=deadbeef",
                },
                method="POST",
            )
            # nosemgrep: python.lang.security.audit.dynamic-urllib-use-detected.dynamic-urllib-use-detected
            with urllib.request.urlopen(req, timeout=5) as resp:  # noqa: S310 - loopback only
                assert resp.status == 200
                assert resp.headers["Content-Type"] == "application/json"
                assert json.loads(resp.read()) == {"status": "received"}
        finally:
            server.shutdown()
            server.server_close()

        # The handler appended a complete record to the module-level buffer.
        assert len(harness.received_webhooks) == 1
        received = harness.received_webhooks[0]
        assert received["path"] == "/webhook"
        # ``dict(self.headers)`` preserves whatever case urllib put on the wire.
        # urllib (via email.message) title-cases custom headers — ``X-GCO-Event``
        # becomes ``X-Gco-Event`` — so assert on that form. The handler's own
        # ``self.headers.get(...)`` call is case-insensitive, so the CLI output
        # prints the original casing regardless.
        assert received["headers"]["X-Gco-Event"] == "job.completed"
        assert received["headers"]["X-Gco-Cluster"] == "gco-us-east-1"
        assert received["headers"]["X-Gco-Region"] == "us-east-1"
        assert received["headers"]["X-Gco-Signature"] == "sha256=deadbeef"
        assert json.loads(received["body"]) == {"event": "job.completed", "job": "test-1"}
        # Timestamp is ISO-8601; parsing it round-trips.
        datetime.fromisoformat(received["timestamp"].replace("Z", "+00:00"))

    def test_do_post_handles_non_json_body(self):
        """Non-JSON bodies still land in received_webhooks verbatim."""
        port = _free_port()
        server = harness.start_local_server(port)
        try:
            req = urllib.request.Request(
                url=f"http://127.0.0.1:{port}/webhook",
                data=b"not json at all",
                headers={"Content-Type": "text/plain"},
                method="POST",
            )
            # nosemgrep: python.lang.security.audit.dynamic-urllib-use-detected.dynamic-urllib-use-detected
            with urllib.request.urlopen(req, timeout=5) as resp:  # noqa: S310
                assert resp.status == 200
        finally:
            server.shutdown()
            server.server_close()
        assert harness.received_webhooks[0]["body"] == "not json at all"

    def test_log_message_is_silenced(self):
        """
        BaseHTTPRequestHandler.log_message writes to stderr by default; our
        override returns None so the recording output stays clean.
        """
        handler = harness.WebhookHandler.__new__(harness.WebhookHandler)
        assert handler.log_message("%s", "silent") is None


# ---------------------------------------------------------------------------
# start_local_server
# ---------------------------------------------------------------------------


class TestStartLocalServer:
    def test_binds_to_requested_port(self):
        port = _free_port()
        server = harness.start_local_server(port)
        try:
            assert server.server_address[1] == port
        finally:
            server.shutdown()
            server.server_close()

    def test_runs_in_a_daemon_thread(self):
        """
        Daemon-thread means pytest can exit cleanly if a test forgets to
        call server_close() — but every test here does call it, so this
        check mainly pins the implementation choice.
        """
        port = _free_port()
        pre_threads = {t.ident for t in threading.enumerate()}
        server = harness.start_local_server(port)
        try:
            new_threads = [
                t for t in threading.enumerate() if t.ident and t.ident not in pre_threads
            ]
            assert any(t.daemon for t in new_threads), (
                "expected start_local_server to spawn at least one daemon thread"
            )
        finally:
            server.shutdown()
            server.server_close()

    def test_server_can_be_reused_after_clean_shutdown(self):
        """
        Port should be released after server_close() so a follow-up test on
        the same port doesn't race on socket reuse. Allow a small delay on
        slower CI hosts where TIME_WAIT takes a tick.
        """
        port = _free_port()
        server = harness.start_local_server(port)
        server.shutdown()
        server.server_close()
        time.sleep(0.05)
        with socket.socket() as s:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            s.bind(("127.0.0.1", port))


# ---------------------------------------------------------------------------
# create_mock_job
# ---------------------------------------------------------------------------


class TestCreateMockJob:
    def test_returns_mock_with_expected_metadata(self):
        job = harness.create_mock_job()
        assert job.metadata.name == "test-webhook-job"
        assert job.metadata.namespace == "gco-jobs"
        assert job.metadata.uid == "test-job-uid-12345"
        assert job.metadata.labels == {"app": "webhook-test", "team": "platform"}

    def test_status_represents_a_completed_job(self):
        job = harness.create_mock_job()
        assert job.status.active == 0
        assert job.status.succeeded == 1
        assert job.status.failed == 0
        assert len(job.status.conditions) == 1
        assert job.status.conditions[0].type == "Complete"
        assert job.status.conditions[0].status == "True"

    def test_completion_time_is_five_minutes_after_start(self):
        """Sanity check on the fixture — the dispatcher computes durations from these."""
        job = harness.create_mock_job()
        delta = job.status.completion_time - job.status.start_time
        assert delta.total_seconds() == 300


# ---------------------------------------------------------------------------
# main() argparse dispatch
# ---------------------------------------------------------------------------


class TestMainDispatch:
    """``main()`` picks between the local-server and external-URL paths."""

    def test_defaults_to_local_server_when_no_url(self):
        mock_local = AsyncMock(return_value=True)
        mock_external = AsyncMock()
        with (
            patch.object(sys, "argv", ["test_webhook_delivery.py"]),
            patch.object(harness, "test_with_local_server", mock_local),
            patch.object(harness, "test_with_external_url", mock_external),
        ):
            exit_code = asyncio.run(harness.main())

        assert exit_code == 0
        mock_local.assert_awaited_once()
        mock_external.assert_not_called()

    def test_uses_external_url_when_provided(self):
        mock_external = AsyncMock(return_value=True)
        mock_local = AsyncMock()
        with (
            patch.object(
                sys,
                "argv",
                ["test_webhook_delivery.py", "--url", "https://example.invalid/webhook"],
            ),
            patch.object(harness, "test_with_external_url", mock_external),
            patch.object(harness, "test_with_local_server", mock_local),
        ):
            exit_code = asyncio.run(harness.main())

        assert exit_code == 0
        mock_external.assert_awaited_once()
        # Positional args: (url, secret). Secret defaults to None.
        assert mock_external.await_args.args == ("https://example.invalid/webhook", None)
        mock_local.assert_not_called()

    def test_passes_secret_to_external_url(self):
        mock_external = AsyncMock(return_value=True)
        with (
            patch.object(
                sys,
                "argv",
                [
                    "test_webhook_delivery.py",
                    "--url",
                    "https://example.invalid/webhook",
                    "--secret",
                    "s3cr3t",
                ],
            ),
            patch.object(harness, "test_with_external_url", mock_external),
        ):
            asyncio.run(harness.main())

        assert mock_external.await_args.args == (
            "https://example.invalid/webhook",
            "s3cr3t",
        )

    def test_returns_nonzero_exit_when_delivery_fails(self):
        mock_local = AsyncMock(return_value=False)
        with (
            patch.object(sys, "argv", ["test_webhook_delivery.py"]),
            patch.object(harness, "test_with_local_server", mock_local),
        ):
            exit_code = asyncio.run(harness.main())
        assert exit_code == 1


# ---------------------------------------------------------------------------
# The two async flows (test_with_local_server / test_with_external_url)
#
# Both construct a real ``WebhookDispatcher`` and drive its private
# ``_dispatch_event``. Here the dispatcher class is swapped for a fake that
# records how it was built and hands back canned ``WebhookDeliveryResult``
# rows, so no port is bound, no DNS is resolved and no HTTP request leaves
# the process. The local-server flow's hard-coded ``start_local_server(8888)``
# is replaced too, so nothing listens on 8888 during the run.
# ---------------------------------------------------------------------------

import hashlib  # noqa: E402 - grouped with the harness it serves
import hmac  # noqa: E402
from types import SimpleNamespace  # noqa: E402
from typing import Any  # noqa: E402
from unittest.mock import MagicMock  # noqa: E402

import gco.services.webhook_dispatcher as dispatcher_module  # noqa: E402
from gco.services.webhook_dispatcher import WebhookDeliveryResult, WebhookEvent  # noqa: E402


def _signature(secret: str, body: str) -> str:
    return "sha256=" + hmac.new(secret.encode(), body.encode(), hashlib.sha256).hexdigest()


def _result(**overrides: Any) -> WebhookDeliveryResult:
    base: dict[str, Any] = {
        "webhook_id": "test-webhook-1",
        "url": "http://localhost:8888/webhook",
        "event": "job.completed",
        "success": True,
        "status_code": 200,
        "attempts": 1,
        "duration_ms": 12.5,
    }
    base.update(overrides)
    return WebhookDeliveryResult(**base)


def _install_fake_dispatcher(
    monkeypatch: pytest.MonkeyPatch,
    results_for: dict[WebhookEvent, list[WebhookDeliveryResult]],
    *,
    deliver_signature: str | None = None,
    deliver_body: str = '{"event": "job.completed"}',
) -> list[Any]:
    """Replace ``WebhookDispatcher`` with a recording fake.

    ``deliver_signature`` simulates the receiver side: when set, every dispatch
    appends a record to ``harness.received_webhooks`` carrying that signature
    header, exactly as the live handler would after an HTTP POST landed.
    """
    created: list[Any] = []

    class FakeDispatcher:
        def __init__(self, **kwargs: Any) -> None:
            self.kwargs = kwargs
            self.dispatched: list[tuple[WebhookEvent, dict[str, Any]]] = []
            created.append(self)

        async def _dispatch_event(self, event: WebhookEvent, job: Any) -> list[Any]:
            self.dispatched.append(
                (
                    event,
                    {
                        "active": job.status.active,
                        "succeeded": job.status.succeeded,
                        "failed": job.status.failed,
                        "completion_time": job.status.completion_time,
                        "conditions": [c.type for c in job.status.conditions],
                    },
                )
            )
            if deliver_signature is not None:
                harness.received_webhooks.append(
                    {
                        "path": "/webhook",
                        "headers": {"X-GCO-Signature": deliver_signature},
                        "body": deliver_body,
                        "timestamp": "2026-02-04T12:05:00+00:00",
                    }
                )
            return results_for[event]

    monkeypatch.setattr(dispatcher_module, "WebhookDispatcher", FakeDispatcher)
    return created


class TestLocalServerFlow:
    def _fake_server(self, monkeypatch: pytest.MonkeyPatch) -> tuple[MagicMock, list[int]]:
        ports: list[int] = []
        server = MagicMock(name="HTTPServer")

        def fake_start(port: int) -> MagicMock:
            ports.append(port)
            return server

        monkeypatch.setattr(harness, "start_local_server", fake_start)
        return server, ports

    def test_successful_delivery_verifies_the_hmac_signature(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        server, ports = self._fake_server(monkeypatch)
        body = '{"event": "job.completed", "job": {"name": "test-webhook-job"}}'
        created = _install_fake_dispatcher(
            monkeypatch,
            {WebhookEvent.JOB_COMPLETED: [_result()]},
            deliver_signature=_signature("test-secret-key", body),
            deliver_body=body,
        )

        ok = asyncio.run(harness.test_with_local_server())

        assert ok is True
        assert ports == [8888]
        server.shutdown.assert_called_once_with()

        (dispatcher,) = created
        assert dispatcher.kwargs["cluster_id"] == "test-cluster"
        assert dispatcher.kwargs["region"] == "us-east-1"
        assert (dispatcher.kwargs["timeout"], dispatcher.kwargs["max_retries"]) == (10, 1)
        store = dispatcher.kwargs["webhook_store"]
        assert store.get_webhooks_for_event.return_value == [
            {
                "id": "test-webhook-1",
                "url": "http://localhost:8888/webhook",
                "events": ["job.completed"],
                "namespace": "gco-jobs",
                "secret": "test-secret-key",
            }
        ]
        # The completed mock job was dispatched once for the completed event.
        assert [event for event, _state in dispatcher.dispatched] == [WebhookEvent.JOB_COMPLETED]
        assert dispatcher.dispatched[0][1]["succeeded"] == 1

        out = capsys.readouterr().out
        assert "WEBHOOK DELIVERY TEST - LOCAL SERVER" in out
        assert "Target URL: http://localhost:8888/webhook" in out
        assert "✓ SUCCESS" in out
        assert "  Webhook ID: test-webhook-1" in out
        assert "  Status Code: 200" in out
        assert "  Duration: 12.5ms" in out
        assert "  Error:" not in out
        assert "✓ Signature verified successfully!" in out
        assert "TEST COMPLETE" in out

    def test_failed_delivery_and_bad_signature_are_reported(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        server, _ports = self._fake_server(monkeypatch)
        _install_fake_dispatcher(
            monkeypatch,
            {
                WebhookEvent.JOB_COMPLETED: [
                    _result(
                        success=False,
                        status_code=None,
                        attempts=2,
                        error="connection refused",
                    )
                ]
            },
            deliver_signature="sha256=not-the-right-digest",
        )

        ok = asyncio.run(harness.test_with_local_server())

        assert ok is False
        server.shutdown.assert_called_once_with()
        out = capsys.readouterr().out
        assert "✗ FAILED" in out
        assert "  Status Code: None" in out
        assert "  Attempts: 2" in out
        assert "  Error: connection refused" in out
        assert "✗ Signature verification failed!" in out
        assert "  Received: sha256=not-the-right-digest" in out
        expected = _signature("test-secret-key", '{"event": "job.completed"}')
        assert f"  Expected: {expected}" in out

    def test_no_results_and_no_received_webhooks_fails_quietly(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        server, _ports = self._fake_server(monkeypatch)
        _install_fake_dispatcher(monkeypatch, {WebhookEvent.JOB_COMPLETED: []})

        ok = asyncio.run(harness.test_with_local_server())

        assert ok is False
        assert harness.received_webhooks == []
        server.shutdown.assert_called_once_with()
        out = capsys.readouterr().out
        assert "DELIVERY RESULTS:" in out
        assert "SIGNATURE VERIFICATION" not in out
        assert "SUCCESS" not in out and "FAILED" not in out


class TestExternalUrlFlow:
    @pytest.fixture(autouse=True)
    def _no_real_sleep(self, monkeypatch: pytest.MonkeyPatch) -> AsyncMock:
        """The flow pauses one second between events; make that instantaneous."""
        sleep = AsyncMock(return_value=None)
        monkeypatch.setattr(harness, "asyncio", SimpleNamespace(sleep=sleep))
        return sleep

    def test_all_three_events_delivered_with_secret(
        self,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
        _no_real_sleep: AsyncMock,
    ) -> None:
        url = "https://webhook.example.invalid/abc"
        results = {
            WebhookEvent.JOB_STARTED: [_result(event="job.started", url=url)],
            WebhookEvent.JOB_COMPLETED: [_result(event="job.completed", url=url)],
            WebhookEvent.JOB_FAILED: [_result(event="job.failed", url=url, status_code=202)],
        }
        created = _install_fake_dispatcher(monkeypatch, results)

        ok = asyncio.run(harness.test_with_external_url(url, "s3cr3t"))

        assert ok is True
        (dispatcher,) = created
        assert dispatcher.kwargs["cluster_id"] == "gco-test-cluster"
        assert (dispatcher.kwargs["timeout"], dispatcher.kwargs["max_retries"]) == (30, 2)
        (config,) = dispatcher.kwargs["webhook_store"].get_webhooks_for_event.return_value
        assert config["url"] == url
        assert config["namespace"] is None
        assert config["secret"] == "s3cr3t"
        assert config["events"] == ["job.completed", "job.failed", "job.started"]

        # The job is reshaped to match each event before it is dispatched.
        events = [event for event, _state in dispatcher.dispatched]
        assert events == [
            WebhookEvent.JOB_STARTED,
            WebhookEvent.JOB_COMPLETED,
            WebhookEvent.JOB_FAILED,
        ]
        started, completed, failed = (state for _event, state in dispatcher.dispatched)
        assert started["conditions"] == [] and started["active"] == 1
        assert started["completion_time"] is None
        assert completed["conditions"] == ["Complete"] and completed["succeeded"] == 1
        assert failed["conditions"] == ["Failed"] and failed["failed"] == 1
        assert failed["completion_time"] is not None

        assert _no_real_sleep.await_count == 3
        _no_real_sleep.assert_awaited_with(1)

        out = capsys.readouterr().out
        assert "WEBHOOK DELIVERY TEST - EXTERNAL URL" in out
        assert "Secret configured: Yes" in out
        for name in ("job.started", "job.completed", "job.failed"):
            assert f"Dispatching {name} event..." in out
        assert out.count("  ✓ Status: 200, Attempts: 1, Duration: 12.5ms") == 2
        assert "  ✓ Status: 202, Attempts: 1, Duration: 12.5ms" in out
        assert "ALL WEBHOOKS DELIVERED SUCCESSFULLY!" in out
        assert f"Check your webhook receiver at: {url}" in out
        assert "Error:" not in out

    def test_a_single_failure_without_secret_fails_the_run(
        self,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        url = "https://webhook.example.invalid/abc"
        results = {
            WebhookEvent.JOB_STARTED: [_result(event="job.started", url=url)],
            WebhookEvent.JOB_COMPLETED: [
                _result(
                    event="job.completed",
                    url=url,
                    success=False,
                    status_code=503,
                    attempts=2,
                    error="HTTP 503",
                )
            ],
            WebhookEvent.JOB_FAILED: [_result(event="job.failed", url=url)],
        }
        created = _install_fake_dispatcher(monkeypatch, results)

        ok = asyncio.run(harness.test_with_external_url(url))

        assert ok is False
        (dispatcher,) = created
        (config,) = dispatcher.kwargs["webhook_store"].get_webhooks_for_event.return_value
        assert "secret" not in config
        # Every event is still attempted after the failure.
        assert len(dispatcher.dispatched) == 3

        out = capsys.readouterr().out
        assert "Secret configured: No" in out
        assert "  ✗ Status: 503, Attempts: 2, Duration: 12.5ms" in out
        assert "    Error: HTTP 503" in out
        assert "SOME WEBHOOKS FAILED - Check errors above" in out
        assert "ALL WEBHOOKS DELIVERED SUCCESSFULLY!" not in out
