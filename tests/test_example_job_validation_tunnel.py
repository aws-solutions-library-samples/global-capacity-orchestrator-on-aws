"""Offline tests for the self-healing SSM tunnel behind the live harnesses' cluster sessions.

A live examples run lost seven examples to a Session Manager port-forward
that stalled for over an hour: the local plugin kept accepting connections,
no TLS handshake completed, five ``gco jobs submit-direct`` calls failed on
``TLS handshake timeout``, and two watchers read submitted Jobs as missing
until their timeouts. These tests pin the repair without AWS or a cluster:
the transport-error classifier; the TLS handshake probe (a completed
handshake, a real self-signed server whose unverifiable certificate still
proves the answer came back, a refused port, and a real listener that accepts
and never answers — the stall); the keeper's reopen on the same local port
through the same instance (generation, evidence events, stop failures, start
and readiness failures with backoff, never after close); the recover /
ensure / watchdog semantics (a healthy tunnel is never reopened for a failed
call, the watchdog waits for two failed handshakes but not for an exited
session) and the watchdog thread's lifecycle; ``SessionKubectl``'s single
repeat and its safe-to-repeat guard; ``cluster_session``'s keeper wiring
(bastion TTL, the shared event list, the keeper closed on every exit, none
without an instance id); ``TunnelSession.instance_id``; and the example
harness's uses of it — unreachable versus missing Job reads, submit-direct's
guarded repeat, the pre-example gate, and the bastion TTL sized to the
selection.
"""

from __future__ import annotations

import contextlib
import json
import re
import socket
import ssl
import subprocess
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import yaml
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

from cli import cluster_tunnel, ephemeral_bastion, ssm_tunnel
from scripts.example_job_validation import actions, drivers, kube, static_checks
from scripts.example_job_validation.specs import EXAMPLE_SPECS, SUBMIT_DIRECT, SUBMIT_SQS
from tests.test_example_job_validation import (
    REPO_ROOT,
    _install_clock,
    _live_ctx,
    _ScriptedKubectl,
    _synthetic_parsed,
)

INSTANCE = "i-0123456789abcdef0"
ENDPOINT = "https://ABC123.gr7.us-east-1.eks.amazonaws.com"
HOST = "ABC123.gr7.us-east-1.eks.amazonaws.com"
STALLED = "TimeoutError: _ssl.c:1063: The handshake operation timed out"
HANDSHAKE_TIMEOUT = "Unable to connect to the server: net/http: TLS handshake timeout"
NOT_FOUND = (1, "", 'Error from server (NotFound): jobs.batch "x" not found')


class _Proc:
    """A tunnel process stand-in: running until ``code`` is set."""

    def __init__(self, name: str, code: int | None = None) -> None:
        self.name = name
        self.code = code

    def poll(self) -> int | None:
        return self.code

    def __repr__(self) -> str:
        return f"_Proc({self.name})"


class _Formatter:
    def __init__(self) -> None:
        self.lines: list[tuple[str, str]] = []

    def print_info(self, message: str) -> None:
        self.lines.append(("info", message))

    def print_success(self, message: str) -> None:
        self.lines.append(("success", message))

    def print_warning(self, message: str) -> None:
        self.lines.append(("warning", message))

    def print_error(self, message: str) -> None:
        self.lines.append(("error", message))


class _Clock:
    def __init__(self) -> None:
        self.now = 1_000.0

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds


class _Harness:
    """A keeper wired to scripted handshakes, a fake clock, and a fake ``ssm_tunnel``.

    ``handshakes`` are answered in order (the last one repeats); set
    ``start_error`` / ``ready_error`` / ``stop_error`` to make that step fail.
    """

    def __init__(self, monkeypatch: pytest.MonkeyPatch, *handshakes: str | None) -> None:
        self.clock = _Clock()
        monkeypatch.setattr(
            kube, "time", SimpleNamespace(monotonic=self.clock.monotonic, sleep=self.clock.sleep)
        )
        self.handshakes = list(handshakes or (None,))
        self.probes: list[tuple[int, str, float]] = []
        monkeypatch.setattr(kube, "_tunnel_handshake_error", self._handshake)
        self.started: list[tuple[str, str, int, str]] = []
        self.stopped: list[_Proc] = []
        self.ready: list[_Proc] = []
        self.start_error: Exception | None = None
        self.ready_error: Exception | None = None
        self.stop_error: Exception | None = None
        monkeypatch.setattr(ssm_tunnel, "start_api_tunnel", self._start)
        monkeypatch.setattr(ssm_tunnel, "stop_api_tunnel", self._stop)
        self.formatter = _Formatter()
        self.events: list[dict[str, Any]] = []
        self.original = _Proc("original")
        self.keeper = kube._TunnelKeeper(
            instance_id=INSTANCE,
            endpoint=ENDPOINT,
            local_port=8443,
            region="us-east-1",
            server_name=HOST,
            process=self.original,  # type: ignore[arg-type]
            wait_ready=self._wait_ready,  # type: ignore[arg-type]
            formatter=self.formatter,
            events=self.events,
        )

    def _handshake(self, port: int, server_name: str, timeout: float) -> str | None:
        self.probes.append((port, server_name, timeout))
        return self.handshakes.pop(0) if len(self.handshakes) > 1 else self.handshakes[0]

    def _start(self, instance_id: str, endpoint: str, local_port: int, region: str) -> _Proc:
        self.started.append((instance_id, endpoint, local_port, region))
        if self.start_error is not None:
            raise self.start_error
        return _Proc(f"reopened-{len(self.started)}")

    def _stop(self, process: _Proc) -> tuple[bytes, bytes]:
        self.stopped.append(process)
        if self.stop_error is not None:
            raise self.stop_error
        return b"", b""

    def _wait_ready(self, process: _Proc) -> None:
        self.ready.append(process)
        if self.ready_error is not None:
            raise self.ready_error


# --------------------------------------------------------------------------
# classifier and probe
# --------------------------------------------------------------------------


class TestTransportErrors:
    @pytest.mark.parametrize(
        "detail",
        [
            HANDSHAKE_TIMEOUT,
            "The connection to the server 127.0.0.1:8443 was refused - did you specify the "
            "right host or port?",
            "dial tcp 127.0.0.1:8443: connect: connection refused",
            "read tcp 127.0.0.1:50000->127.0.0.1:8443: read: connection reset by peer",
            "dial tcp 127.0.0.1:8443: i/o timeout",
            # What the five failed submit-direct calls printed, verbatim in shape.
            'error validating data: failed to download openapi: Get "https://127.0.0.1:8443/'
            'openapi/v2?timeout=32s": net/http: TLS handshake timeout',
        ],
    )
    def test_failures_that_never_crossed_the_tunnel_are_recognized(self, detail: str) -> None:
        assert kube.is_tunnel_transport_error(detail)
        assert kube.is_tunnel_transport_error(detail.upper())

    @pytest.mark.parametrize(
        "detail",
        [
            NOT_FOUND[2],
            "Error from server (Forbidden): jobs.batch is forbidden",
            'error: the server doesn\'t have a resource type "trainjobs"',
            "",
        ],
    )
    def test_answers_from_the_api_server_are_not(self, detail: str) -> None:
        assert not kube.is_tunnel_transport_error(detail)


def _self_signed_server_context(tmp_path: Path) -> ssl.SSLContext:
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, HOST)])
    now = datetime.now(UTC)
    certificate = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=1))
        .not_valid_after(now + timedelta(hours=1))
        .add_extension(x509.SubjectAlternativeName([x509.DNSName(HOST)]), critical=False)
        .sign(key, hashes.SHA256())
    )
    cert_path = tmp_path / "server.pem"
    key_path = tmp_path / "server.key"
    cert_path.write_bytes(certificate.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(cert_path, key_path)
    return context


class TestHandshakeProbe:
    def test_a_completed_handshake_is_healthy(self, monkeypatch: pytest.MonkeyPatch) -> None:
        seen: dict[str, Any] = {}

        class _Managed:
            def __enter__(self) -> _Managed:
                return self

            def __exit__(self, *exc: object) -> None:
                seen.setdefault("closed", []).append(self)

        connection = _Managed()

        class _Context:
            minimum_version: Any = None

            def wrap_socket(self, raw: Any, *, server_hostname: str) -> _Managed:
                seen["wrapped"] = (raw, server_hostname, self.minimum_version)
                return _Managed()

        def create_connection(address: tuple[str, int], timeout: float) -> _Managed:
            seen["address"] = (address, timeout)
            return connection

        monkeypatch.setattr(kube.socket, "create_connection", create_connection)
        monkeypatch.setattr(kube.ssl, "create_default_context", _Context)

        assert kube._tunnel_handshake_error(8443, HOST, 10.0) is None
        assert seen["address"] == (("127.0.0.1", 8443), 10.0)
        # Nothing older than TLS 1.2 is ever offered, even for a probe.
        assert seen["wrapped"] == (connection, HOST, ssl.TLSVersion.TLSv1_2)
        assert len(seen["closed"]) == 2

    def test_a_real_server_whose_certificate_cannot_be_verified_still_answered(
        self, tmp_path: Path
    ) -> None:
        """The EKS API's certificate chains to the cluster CA, never the system store."""
        server_context = _self_signed_server_context(tmp_path)
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            listener.listen(1)
            port = listener.getsockname()[1]

            def serve() -> None:
                connection, _address = listener.accept()
                with connection, contextlib.suppress(OSError):
                    server_context.wrap_socket(connection, server_side=True).close()

            server = threading.Thread(target=serve, daemon=True)
            server.start()
            assert kube._tunnel_handshake_error(port, HOST, 5.0) is None
            server.join(timeout=5)

    def test_a_listener_that_accepts_and_never_answers_is_the_stall(self) -> None:
        """What the stalled Session Manager plugin did: accept, then relay nothing."""
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            listener.listen(1)
            error = kube._tunnel_handshake_error(listener.getsockname()[1], HOST, 0.2)
        assert error is not None
        assert error.startswith("TimeoutError: ")

    def test_a_port_nothing_listens_on_is_named(self) -> None:
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]
        error = kube._tunnel_handshake_error(port, HOST, 1.0)
        assert error is not None
        assert error.startswith("ConnectionRefusedError: ")


# --------------------------------------------------------------------------
# the keeper
# --------------------------------------------------------------------------


class TestKeeperRecover:
    def test_a_call_that_started_before_a_reopen_is_repeated_without_probing(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        harness = _Harness(monkeypatch)
        harness.keeper.generation = 3

        assert harness.keeper.recover(2) is True
        assert harness.probes == []
        assert harness.started == []

    def test_a_healthy_tunnel_is_never_reopened_for_a_failed_call(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        harness = _Harness(monkeypatch, None)

        assert harness.keeper.recover(0) is False
        assert harness.probes == [(8443, HOST, 10.0)]
        assert harness.started == [] and harness.events == []

    def test_a_stalled_tunnel_is_reopened_on_the_same_port_through_the_same_instance(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        harness = _Harness(monkeypatch, STALLED)

        assert harness.keeper.recover(0) is True

        assert harness.stopped == [harness.original]
        assert harness.started == [(INSTANCE, ENDPOINT, 8443, "us-east-1")]
        reopened = harness.keeper.process
        assert isinstance(reopened, _Proc) and reopened.name == "reopened-1"
        assert harness.ready == [reopened]
        assert harness.keeper.generation == 1
        [event] = harness.events
        assert set(event) == {"at", "reason", "result"}
        assert re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ", event["at"])
        assert event["reason"] == (
            "a call through the tunnel failed and no TLS handshake through 127.0.0.1:8443 "
            f"within 10s ({STALLED})"
        )
        assert event["result"] == "reopened"
        assert [level for level, _ in harness.formatter.lines] == ["warning", "success"]

    def test_an_exited_session_is_reopened_without_a_handshake(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        harness = _Harness(monkeypatch)
        harness.original.code = 255

        assert harness.keeper.recover(0) is True
        assert harness.probes == []
        assert harness.events[0]["reason"] == (
            "a call through the tunnel failed and the SSM session exited with code 255"
        )


class TestKeeperReopenFailures:
    def test_a_session_that_cannot_start_backs_off_before_trying_again(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        harness = _Harness(monkeypatch, STALLED)
        harness.start_error = RuntimeError("TargetNotConnected: the bastion is gone")

        assert harness.keeper.recover(0) is False
        assert harness.keeper.process is None
        assert harness.stopped == [harness.original]
        assert harness.events[-1]["result"] == "failed"
        assert harness.events[-1]["error"] == (
            "RuntimeError: TargetNotConnected: the bastion is gone"
        )
        assert harness.formatter.lines[-1] == (
            "error",
            "Could not reopen the SSM tunnel: RuntimeError: TargetNotConnected: the bastion is gone",
        )

        # Inside the backoff nothing is started, however many callers ask.
        harness.clock.now += kube._TUNNEL_REOPEN_BACKOFF_SECONDS - 1
        assert harness.keeper.recover(0) is False
        assert len(harness.started) == 1 and len(harness.events) == 1

        harness.clock.now += 1
        harness.start_error = None
        assert harness.keeper.recover(0) is True
        assert len(harness.started) == 2
        # The failed attempt left no process behind to stop.
        assert harness.stopped == [harness.original]
        assert [event["result"] for event in harness.events] == ["failed", "reopened"]

    def test_a_session_the_api_never_answers_through_is_kept_but_not_counted(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        harness = _Harness(monkeypatch, STALLED)
        harness.ready_error = RuntimeError("Kubernetes API did not become ready")

        assert harness.keeper.recover(0) is False
        assert harness.keeper.generation == 0
        assert isinstance(harness.keeper.process, _Proc)
        assert harness.keeper.process.name == "reopened-1"
        assert harness.events[-1]["error"] == "RuntimeError: Kubernetes API did not become ready"

    def test_a_stale_session_that_will_not_stop_is_recorded_and_replaced(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        harness = _Harness(monkeypatch, STALLED)
        harness.stop_error = RuntimeError("process tree did not exit")

        assert harness.keeper.recover(0) is True
        assert harness.events[-1]["stop_error"] == "process tree did not exit"
        assert harness.events[-1]["result"] == "reopened"

    def test_nothing_is_opened_once_the_session_is_closing(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        harness = _Harness(monkeypatch, STALLED)
        harness.keeper.close()

        assert harness.keeper.recover(0) is False
        assert harness.started == []
        with pytest.raises(kube.TunnelUnavailableError, match="the cluster session is closing"):
            harness.keeper.ensure()


class TestKeeperEnsure:
    def test_a_healthy_tunnel_is_left_alone(self, monkeypatch: pytest.MonkeyPatch) -> None:
        harness = _Harness(monkeypatch, None)
        harness.keeper.ensure()
        assert harness.started == []

    def test_a_broken_tunnel_is_reopened(self, monkeypatch: pytest.MonkeyPatch) -> None:
        harness = _Harness(monkeypatch, STALLED)
        harness.keeper.ensure()
        assert harness.keeper.generation == 1
        assert harness.events[0]["reason"].startswith("no TLS handshake through 127.0.0.1:8443")

    def test_a_tunnel_that_cannot_be_reopened_is_an_error(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        harness = _Harness(monkeypatch, STALLED)
        harness.start_error = RuntimeError("TargetNotConnected")

        with pytest.raises(kube.TunnelUnavailableError) as excinfo:
            harness.keeper.ensure()
        assert str(excinfo.value) == (
            "the SSM tunnel to the Kubernetes API is broken (no TLS handshake through "
            f"127.0.0.1:8443 within 10s ({STALLED})) and could not be reopened: "
            "RuntimeError: TargetNotConnected"
        )


class TestKeeperWatchdog:
    def test_one_failed_handshake_is_not_enough_two_in_a_row_are(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        harness = _Harness(monkeypatch, STALLED)

        harness.keeper.check()
        assert harness.started == []
        harness.keeper.check()
        assert len(harness.started) == 1
        assert harness.events[0]["reason"].startswith("watchdog: no TLS handshake")

    def test_a_good_handshake_resets_the_count(self, monkeypatch: pytest.MonkeyPatch) -> None:
        harness = _Harness(monkeypatch, STALLED, None, STALLED, None)

        for _ in range(4):
            harness.keeper.check()
        assert harness.started == []

    def test_an_exited_session_is_reopened_on_the_first_pass(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        harness = _Harness(monkeypatch)
        harness.original.code = 1

        harness.keeper.check()
        assert harness.events[0]["reason"] == "watchdog: the SSM session exited with code 1"
        assert harness.keeper.generation == 1

    def test_the_thread_probes_until_closed_and_survives_a_bad_probe(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        harness = _Harness(monkeypatch)
        monkeypatch.setattr(kube, "_TUNNEL_CHECK_INTERVAL_SECONDS", 0.001)
        probed = threading.Event()
        calls: list[int] = []

        def handshake(port: int, server_name: str, timeout: float) -> str | None:
            calls.append(port)
            if len(calls) == 1:
                raise ValueError("probe blew up")
            if len(calls) >= 3:
                probed.set()
            return None

        monkeypatch.setattr(kube, "_tunnel_handshake_error", handshake)

        harness.keeper.start()
        assert probed.wait(timeout=5)
        harness.keeper.close()

        assert harness.keeper._thread is not None
        assert not harness.keeper._thread.is_alive()
        assert harness.keeper._thread.name == "gco-tunnel-watchdog"
        assert harness.keeper._thread.daemon is True
        assert (
            "warning",
            "SSM tunnel watchdog check failed: ValueError: probe blew up",
        ) in harness.formatter.lines
        # Healthy probes after the bad one: nothing was reopened.
        assert harness.started == []


class TestKeeperClose:
    def test_a_reopened_session_is_stopped_and_the_original_left_to_its_opener(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        harness = _Harness(monkeypatch, STALLED)
        harness.keeper.close()
        assert harness.stopped == []

        harness = _Harness(monkeypatch, STALLED)
        assert harness.keeper.recover(0) is True
        reopened = harness.keeper.process
        harness.keeper.close()
        assert harness.stopped == [harness.original, reopened]

    def test_a_reopened_session_that_will_not_stop_is_reported(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        harness = _Harness(monkeypatch, STALLED)
        assert harness.keeper.recover(0) is True
        harness.stop_error = RuntimeError("stuck")

        harness.keeper.close()
        assert harness.formatter.lines[-1] == (
            "error",
            "Could not stop the reopened SSM tunnel: stuck",
        )

    def test_no_session_left_after_a_failed_reopen_means_nothing_to_stop(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        harness = _Harness(monkeypatch, STALLED)
        harness.start_error = RuntimeError("TargetNotConnected")
        assert harness.keeper.recover(0) is False

        harness.keeper.close()
        assert harness.stopped == [harness.original]


# --------------------------------------------------------------------------
# the runner
# --------------------------------------------------------------------------


class _KeeperStub:
    def __init__(self, *, recovers: bool = True, generation: int = 4) -> None:
        self.recovers = recovers
        self.generation = generation
        self.recovered: list[int] = []
        self.ensured = 0

    def recover(self, since: int) -> bool:
        self.recovered.append(since)
        return self.recovers

    def ensure(self) -> None:
        self.ensured += 1


def _calls(*results: tuple[int, str, str]) -> tuple[list[int], Any]:
    made: list[int] = []
    queue = list(results)

    def call() -> tuple[int, str, str]:
        made.append(len(made))
        return queue.pop(0) if len(queue) > 1 else queue[0]

    return made, call


class TestSessionKubectl:
    def test_without_a_keeper_every_call_runs_once(self) -> None:
        made, call = _calls((1, "", HANDSHAKE_TIMEOUT))
        runner = kube.SessionKubectl(None)
        assert runner.through_tunnel(call) == (1, "", HANDSHAKE_TIMEOUT)
        assert made == [0]
        runner.ensure_tunnel()  # a no-op without a keeper

    @pytest.mark.parametrize(
        "result", [(0, "ok", ""), (1, "", NOT_FOUND[2])], ids=("success", "api-answer")
    )
    def test_calls_the_tunnel_did_not_break_are_never_repeated(
        self, result: tuple[int, str, str]
    ) -> None:
        keeper = _KeeperStub()
        made, call = _calls(result)
        runner = kube.SessionKubectl(None, keeper)  # type: ignore[arg-type]
        assert runner.through_tunnel(call) == result
        assert made == [0]
        assert keeper.recovered == []

    @pytest.mark.parametrize(
        "failure", [(1, "", HANDSHAKE_TIMEOUT), (1, HANDSHAKE_TIMEOUT, "")], ids=("err", "out")
    )
    def test_a_call_the_tunnel_broke_is_repeated_once_after_recovery(
        self, failure: tuple[int, str, str]
    ) -> None:
        keeper = _KeeperStub(generation=4)
        made, call = _calls(failure, (0, "ok", ""))
        runner = kube.SessionKubectl(None, keeper)  # type: ignore[arg-type]
        assert runner.through_tunnel(call) == (0, "ok", "")
        assert made == [0, 1]
        # The generation is read before the call, so a reopen during it counts.
        assert keeper.recovered == [4]

    def test_a_second_failure_is_returned_not_repeated_again(self) -> None:
        keeper = _KeeperStub()
        made, call = _calls((1, "", HANDSHAKE_TIMEOUT))
        runner = kube.SessionKubectl(None, keeper)  # type: ignore[arg-type]
        assert runner.through_tunnel(call) == (1, "", HANDSHAKE_TIMEOUT)
        assert made == [0, 1]

    def test_no_repeat_when_the_keeper_finds_the_tunnel_healthy(self) -> None:
        keeper = _KeeperStub(recovers=False)
        made, call = _calls((1, "", HANDSHAKE_TIMEOUT))
        runner = kube.SessionKubectl(None, keeper)  # type: ignore[arg-type]
        assert runner.through_tunnel(call) == (1, "", HANDSHAKE_TIMEOUT)
        assert made == [0]

    def test_the_guard_is_asked_after_recovery_and_can_refuse(self) -> None:
        keeper = _KeeperStub()
        asked: list[list[int]] = []
        made, call = _calls((1, "", HANDSHAKE_TIMEOUT), (0, "ok", ""))

        def guard() -> bool:
            asked.append(list(keeper.recovered))
            return False

        runner = kube.SessionKubectl(None, keeper)  # type: ignore[arg-type]
        assert runner.through_tunnel(call, safe_to_repeat=guard) == (1, "", HANDSHAKE_TIMEOUT)
        assert made == [0]
        assert asked == [[4]]

    def test_calling_the_runner_repeats_the_same_kubectl_invocation(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        kubeconfig = tmp_path / "kubeconfig"
        runs: list[tuple[list[str], dict[str, Any]]] = []
        answers = [(1, "", HANDSHAKE_TIMEOUT), (0, "{}", "")]

        def fake_run(command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
            runs.append((command, kwargs))
            code, out, err = answers.pop(0)
            return subprocess.CompletedProcess(command, code, stdout=out, stderr=err)

        monkeypatch.setattr(kube.subprocess, "run", fake_run)
        keeper = _KeeperStub()
        runner = kube.SessionKubectl(kubeconfig, keeper)  # type: ignore[arg-type]

        assert runner("apply", "--filename", "-", input="{}", timeout=30) == (0, "{}", "")
        assert [command for command, _ in runs] == [
            ["kubectl", "--kubeconfig", str(kubeconfig), "apply", "--filename", "-"]
        ] * 2
        assert all(kwargs["input"] == "{}" and kwargs["timeout"] == 30 for _, kwargs in runs)
        assert all(kwargs["env"]["KUBECONFIG"] == str(kubeconfig) for _, kwargs in runs)

    def test_ensure_tunnel_asks_the_keeper(self) -> None:
        keeper = _KeeperStub()
        kube.SessionKubectl(None, keeper).ensure_tunnel()  # type: ignore[arg-type]
        assert keeper.ensured == 1

    def test_module_helpers_leave_any_other_runner_alone(self) -> None:
        made, call = _calls((1, "", HANDSHAKE_TIMEOUT))
        assert kube.through_tunnel(lambda *a, **k: (0, "", ""), call) == (
            1,
            "",
            HANDSHAKE_TIMEOUT,
        )
        assert made == [0]
        kube.ensure_tunnel(lambda *a, **k: (0, "", ""))

    def test_module_helpers_delegate_to_a_session_runner(self) -> None:
        keeper = _KeeperStub()
        runner = kube.SessionKubectl(None, keeper)  # type: ignore[arg-type]
        made, call = _calls((1, "", HANDSHAKE_TIMEOUT), (0, "ok", ""))
        assert kube.through_tunnel(runner, call, safe_to_repeat=lambda: True) == (0, "ok", "")
        assert made == [0, 1]
        kube.ensure_tunnel(runner)
        assert keeper.ensured == 1


# --------------------------------------------------------------------------
# cluster_session wiring
# --------------------------------------------------------------------------


def _fake_cluster_commands(path: Path) -> Any:
    """``subprocess.run`` for access, update-kubeconfig (writes ``path``), and kubectl."""

    def fake_run(command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        if command[:3] == ["aws", "eks", "update-kubeconfig"]:
            config = {
                "apiVersion": "v1",
                "clusters": [
                    {
                        "name": "arn:aws:eks:us-east-1:111111111111:cluster/test-cluster",
                        "cluster": {"server": ENDPOINT, "certificate-authority-data": "CA"},
                    }
                ],
            }
            path.write_text(yaml.safe_dump(config), encoding="utf-8")
            path.chmod(0o600)
        return subprocess.CompletedProcess(command, 0, stdout="ok", stderr="")

    return fake_run


def _tunnel_session(*, instance_id: str | None = INSTANCE) -> cluster_tunnel.TunnelSession:
    return cluster_tunnel.TunnelSession(
        server="https://127.0.0.1:8443",
        tls_server_name=HOST,
        plan=cluster_tunnel.TunnelPlan(
            cluster="test-cluster",
            region="us-east-1",
            endpoint=ENDPOINT,
            public=False,
            private=True,
        ),
        active=True,
        process=_Proc("original"),  # type: ignore[arg-type]
        instance_id=instance_id,
    )


class TestClusterSessionKeeper:
    @staticmethod
    def _install(
        monkeypatch: pytest.MonkeyPatch, tmp_path: Path, session: cluster_tunnel.TunnelSession
    ) -> tuple[Path, dict[str, Any], list[tuple[str, Any]]]:
        report_dir = tmp_path / "report"
        report_dir.mkdir(mode=0o700)
        path = report_dir / "kubeconfig"
        monkeypatch.setattr(kube.subprocess, "run", _fake_cluster_commands(path))
        opened: dict[str, Any] = {}

        @contextlib.contextmanager
        def fake_tunnel(formatter: Any, **kwargs: Any):
            opened.update(kwargs)
            yield session

        monkeypatch.setattr(cluster_tunnel, "open_api_server_tunnel", fake_tunnel)
        lifecycle: list[tuple[str, Any]] = []
        monkeypatch.setattr(
            kube._TunnelKeeper, "start", lambda self: lifecycle.append(("start", self))
        )
        monkeypatch.setattr(
            kube._TunnelKeeper, "close", lambda self: lifecycle.append(("close", self))
        )
        return path, opened, lifecycle

    def test_a_tunnelled_session_is_kept_for_its_lifetime(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        session = _tunnel_session()
        path, opened, lifecycle = self._install(monkeypatch, tmp_path, session)
        events: list[dict[str, Any]] = []

        with kube.cluster_session(
            REPO_ROOT,
            "test-cluster",
            "us-east-1",
            kubeconfig_path=path,
            bastion_ttl_minutes=600,
            tunnel_events=events,
        ) as kubectl:
            assert isinstance(kubectl, kube.SessionKubectl)
            keeper = kubectl.keeper
            assert isinstance(keeper, kube._TunnelKeeper)
            assert lifecycle == [("start", keeper)]
            assert keeper.events is events
            assert keeper.process is session.process
            assert (keeper._instance_id, keeper._endpoint, keeper._local_port) == (
                INSTANCE,
                ENDPOINT,
                8443,
            )
            assert (keeper._region, keeper._server_name) == ("us-east-1", HOST)
            # A reopened session is readied through a kubectl that never repeats.
            keeper._wait_ready(_Proc("reopened"))  # type: ignore[arg-type]

        assert lifecycle == [("start", keeper), ("close", keeper)]
        assert opened["bastion_ttl_minutes"] == 600
        assert opened["via_ssm"] == cluster_tunnel.AUTO_BASTION

    def test_the_keeper_is_closed_when_the_body_fails(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        path, _opened, lifecycle = self._install(monkeypatch, tmp_path, _tunnel_session())

        with (
            pytest.raises(RuntimeError, match="example crashed"),
            kube.cluster_session(REPO_ROOT, "test-cluster", "us-east-1", kubeconfig_path=path),
        ):
            raise RuntimeError("example crashed")
        assert [step for step, _keeper in lifecycle] == ["start", "close"]
        # No list given: the keeper kept its own.
        assert lifecycle[0][1].events == []

    def test_a_tunnel_without_an_instance_id_gets_no_keeper(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        path, _opened, lifecycle = self._install(
            monkeypatch, tmp_path, _tunnel_session(instance_id=None)
        )
        with kube.cluster_session(
            REPO_ROOT, "test-cluster", "us-east-1", kubeconfig_path=path
        ) as kubectl:
            assert isinstance(kubectl, kube.SessionKubectl)
            assert kubectl.keeper is None
        assert lifecycle == []


class TestTunnelSessionInstanceId:
    @staticmethod
    def _private(monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            cluster_tunnel.kubectl_helpers,
            "describe_cluster_access",
            lambda cluster, region: {"endpoint": ENDPOINT, "public": False, "private": True},
        )
        monkeypatch.setattr(ssm_tunnel, "start_api_tunnel", lambda *a, **k: _Proc("tunnel"))
        monkeypatch.setattr(ssm_tunnel, "stop_api_tunnel", lambda process: (b"", b""))

    def test_the_session_names_the_instance_it_tunnels_through(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._private(monkeypatch)
        with cluster_tunnel.open_api_server_tunnel(
            _Formatter(), cluster="gco-us-east-1", region="us-east-1", via_ssm=INSTANCE
        ) as session:
            assert session.instance_id == INSTANCE

    def test_an_auto_bastion_is_named_and_its_ttl_forwarded(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._private(monkeypatch)
        provisioned: list[int] = []

        def provision(formatter: Any, cluster: str, region: str, ttl: int, *rest: Any) -> str:
            provisioned.append(ttl)
            return "i-0aaaaaaaaaaaaaaaa"

        monkeypatch.setattr(cluster_tunnel, "provision_bastion", provision)
        monkeypatch.setattr(cluster_tunnel, "teardown_bastion", lambda *a: None)
        with cluster_tunnel.open_api_server_tunnel(
            _Formatter(),
            cluster="gco-us-east-1",
            region="us-east-1",
            via_ssm=cluster_tunnel.AUTO_BASTION,
            bastion_ttl_minutes=600,
            assume_yes=True,
        ) as session:
            assert session.instance_id == "i-0aaaaaaaaaaaaaaaa"
        assert provisioned == [600]

    def test_no_tunnel_means_no_instance(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self._private(monkeypatch)
        with cluster_tunnel.open_api_server_tunnel(
            _Formatter(), cluster="gco-us-east-1", region="us-east-1", via_ssm=None
        ) as session:
            assert session.active is False
            assert session.instance_id is None


# --------------------------------------------------------------------------
# the example harness's uses
# --------------------------------------------------------------------------


class _ScriptedSession(kube.SessionKubectl):
    """A session runner whose kubectl invocations are scripted."""

    def __init__(self, routes: dict[tuple[str, ...], object], keeper: Any) -> None:
        super().__init__(None, keeper)
        self.scripted = _ScriptedKubectl(routes)

    def run_once(self, *args: str, timeout: float = 120, **kwargs: Any) -> tuple[int, str, str]:
        return self.scripted(*args, timeout=timeout, **kwargs)


def _record_cli(monkeypatch: pytest.MonkeyPatch, *answers: tuple[int, str, str]) -> list[list[str]]:
    calls: list[list[str]] = []
    queue = list(answers)

    def fake_run_cli(args: list[str], repo_root: Path, timeout: int = 600) -> tuple[int, str, str]:
        calls.append(list(args))
        return queue.pop(0) if len(queue) > 1 else queue[0]

    monkeypatch.setattr(drivers, "_run_cli", fake_run_cli)
    return calls


_SUBMIT_FAILED = (
    1,
    "",
    "✗ Failed to submit job directly: kubectl apply failed: error: error validating data: "
    'failed to download openapi: Get "https://127.0.0.1:8443/openapi/v2?timeout=32s": '
    "net/http: TLS handshake timeout",
)


class TestSubmitDirectRepeat:
    def test_a_submission_the_tunnel_broke_is_repeated_once_nothing_landed(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        parsed = static_checks.parse_example(REPO_ROOT, "efs-output-job")
        assert parsed.spec.submission == SUBMIT_DIRECT
        calls = _record_cli(monkeypatch, _SUBMIT_FAILED, (0, "job.batch/efs created", ""))
        keeper = _KeeperStub()
        kubectl = _ScriptedSession({("get", "job"): NOT_FOUND}, keeper)

        evidence = drivers.submit_example(
            parsed, parsed.path, repo_root=REPO_ROOT, region="us-east-1", kubectl=kubectl
        )

        assert len(calls) == 2 and calls[0] == calls[1]
        assert evidence["output"] == "job.batch/efs created"
        assert keeper.recovered == [keeper.generation]
        jobs = [doc["metadata"]["name"] for doc in parsed.documents if doc.get("kind") == "Job"]
        assert jobs
        assert [args[2] for args in kubectl.scripted.commands("get", "job")] == jobs

    def test_a_job_that_already_landed_is_never_submitted_twice(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """submit-direct renames a second submission of a running Job: a duplicate."""
        parsed = static_checks.parse_example(REPO_ROOT, "efs-output-job")
        calls = _record_cli(monkeypatch, _SUBMIT_FAILED)
        running = (0, json.dumps({"status": {"active": 1}}), "")
        kubectl = _ScriptedSession({("get", "job"): running}, _KeeperStub())

        with pytest.raises(drivers.ExampleValidationError, match="TLS handshake timeout"):
            drivers.submit_example(
                parsed, parsed.path, repo_root=REPO_ROOT, region="us-east-1", kubectl=kubectl
            )
        assert len(calls) == 1

    def test_a_job_the_reads_cannot_see_is_not_proven_absent(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        parsed = static_checks.parse_example(REPO_ROOT, "efs-output-job")
        calls = _record_cli(monkeypatch, _SUBMIT_FAILED)
        kubectl = _ScriptedSession({("get", "job"): (1, "", HANDSHAKE_TIMEOUT)}, _KeeperStub())

        with pytest.raises(drivers.ExampleValidationError):
            drivers.submit_example(
                parsed, parsed.path, repo_root=REPO_ROOT, region="us-east-1", kubectl=kubectl
            )
        assert len(calls) == 1

    def test_an_example_without_jobs_is_repeatable(self, monkeypatch: pytest.MonkeyPatch) -> None:
        parsed = static_checks.parse_example(REPO_ROOT, "inference-vllm")
        assert parsed.spec.submission == SUBMIT_DIRECT
        assert not [doc for doc in parsed.documents if doc.get("kind") == "Job"]
        calls = _record_cli(monkeypatch, _SUBMIT_FAILED, (0, "deployment created", ""))
        kubectl = _ScriptedSession({}, _KeeperStub())

        drivers.submit_example(
            parsed, parsed.path, repo_root=REPO_ROOT, region="us-east-1", kubectl=kubectl
        )
        assert len(calls) == 2

    def test_submissions_that_do_not_use_the_tunnel_are_never_repeated(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        parsed = static_checks.parse_example(REPO_ROOT, "simple-job")
        assert parsed.spec.submission == SUBMIT_SQS
        calls = _record_cli(monkeypatch, (1, "", "connection reset by peer"))
        keeper = _KeeperStub()
        kubectl = _ScriptedSession({}, keeper)

        with pytest.raises(drivers.ExampleValidationError, match="connection reset by peer"):
            drivers.submit_example(
                parsed, parsed.path, repo_root=REPO_ROOT, region="us-east-1", kubectl=kubectl
            )
        assert len(calls) == 1
        assert keeper.recovered == []


class TestUnreachableJobReads:
    def test_a_watcher_times_out_naming_the_read_it_could_not_make(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _install_clock(monkeypatch)
        parsed = _synthetic_parsed(
            "simple-job",
            EXAMPLE_SPECS["simple-job"],
            [{"kind": "Job", "metadata": {"name": "hello", "namespace": "gco-jobs"}}],
        )
        kubectl = _ScriptedKubectl(
            {
                ("get", "job"): (1, "", HANDSHAKE_TIMEOUT),
                ("get", "events"): (1, "", HANDSHAKE_TIMEOUT),
                ("get", "pods"): (1, "", HANDSHAKE_TIMEOUT),
            }
        )
        with pytest.raises(drivers.ExampleValidationError) as excinfo:
            drivers.wait_jobs_complete(parsed, kubectl, timeout=1)
        assert str(excinfo.value) == (
            f"timeout after 1s: gco-jobs/hello=unreachable ({HANDSHAKE_TIMEOUT}); pods: "
        )

    def test_cleanup_never_takes_an_unreadable_job_for_a_deleted_one(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _install_clock(monkeypatch)
        kubectl = _ScriptedKubectl({("get", "job"): (1, "", HANDSHAKE_TIMEOUT)})
        with pytest.raises(drivers.ExampleValidationError) as excinfo:
            drivers._wait_jobs_gone(kubectl, [("gco-jobs", "hello")], timeout=1)
        assert str(excinfo.value) == (
            "derived Job(s) still present after cleanup: gco-jobs/hello (unreachable)"
        )


class TestPreExampleGate:
    def test_an_unreachable_api_fails_the_example_before_anything_is_submitted(self) -> None:
        class _Unavailable(_KeeperStub):
            def ensure(self) -> None:
                raise kube.TunnelUnavailableError("the SSM tunnel is broken: TargetNotConnected")

        kubectl = _ScriptedSession({}, _Unavailable())
        result = actions._run_one_example(_live_ctx(), "simple-job", "us-east-1", kubectl)

        assert result.status == "failed"
        assert result.detail == (
            "the cluster API was unreachable before the example started: "
            "the SSM tunnel is broken: TargetNotConnected"
        )
        assert result.evidence == {} and result.mutations == {}
        assert kubectl.scripted.calls == []


class TestBastionTtl:
    def test_a_short_selection_keeps_the_default_floor(self) -> None:
        assert actions._bastion_ttl_minutes(["simple-job"], 1) == (
            ephemeral_bastion.DEFAULT_TTL_MINUTES
        )

    @pytest.mark.parametrize(("workers", "minutes"), [(1, 280), (3, 140)])
    def test_the_worst_case_over_the_workers_plus_the_longest_example(
        self, workers: int, minutes: int
    ) -> None:
        names = ["inference-sglang", "inference-triton", "inference-vllm"]
        assert {EXAMPLE_SPECS[name].timeout_seconds for name in names} == {2400}
        assert actions._bastion_ttl_minutes(names, workers) == minutes

    def test_the_whole_catalog_is_capped_at_what_a_bastion_accepts(self) -> None:
        minutes = actions._bastion_ttl_minutes(sorted(EXAMPLE_SPECS), 1)
        assert minutes == ephemeral_bastion.MAX_TTL_MINUTES
        assert ephemeral_bastion._validate_ttl(minutes) == minutes
        with pytest.raises(ValueError, match="between 5 and 1440"):
            ephemeral_bastion._validate_ttl(ephemeral_bastion.MAX_TTL_MINUTES + 1)
        with pytest.raises(ValueError, match="between 5 and 1440"):
            ephemeral_bastion._validate_ttl(ephemeral_bastion.MIN_TTL_MINUTES - 1)
