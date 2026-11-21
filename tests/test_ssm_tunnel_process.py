"""The SSM tunnel against a real stand-in ``aws`` process (cli/ssm_tunnel.py).

The stand-in is an executable script named ``aws`` first on ``PATH``. It reads
the local port from the ``--parameters`` JSON exactly as the Session Manager
plugin does, then behaves as ``FAKE_SSM_MODE`` says:

* ``listen``: bind the port, print the plugin's "Port N opened for sessionId"
  line, and accept connections, printing a line for each one as the plugin does;
* ``unannounced``: bind the port but never print that line;
* ``chatty``: like ``listen``, then write 4 MiB to stdout and to stderr before
  touching the ``chatter-done`` marker.

Every test uses a port that was free a moment earlier, so the readiness rule,
the refusal of a port another process holds, and the draining of the output
pipes are exercised end to end rather than through mocked sockets.
"""

from __future__ import annotations

import os
import socket
import stat
import subprocess
import sys
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from cli import ssm_tunnel

pytestmark = pytest.mark.skipif(os.name != "posix", reason="POSIX process groups and shebangs")

_ENDPOINT = "https://ABC.gr7.us-east-1.eks.amazonaws.com"

_FAKE_AWS = """\
import json
import os
import socket
import sys
from pathlib import Path

args = sys.argv[1:]
port = int(json.loads(args[args.index("--parameters") + 1])["localPortNumber"][0])
marker_dir = Path(os.environ["FAKE_SSM_DIR"])
(marker_dir / "started").write_text(str(os.getpid()))
mode = os.environ.get("FAKE_SSM_MODE", "listen")
print("Starting session with SessionId: test-session", flush=True)
listener = socket.socket()
listener.bind(("127.0.0.1", port))
listener.listen()
if mode != "unannounced":
    print(f"Port {port} opened for sessionId test-session.", flush=True)
    print("Waiting for connections...", flush=True)
if mode == "chatty":
    blob = b"x" * 65536
    for _ in range(64):
        sys.stdout.buffer.write(blob)
        sys.stdout.buffer.flush()
        sys.stderr.buffer.write(blob)
        sys.stderr.buffer.flush()
    (marker_dir / "chatter-done").touch()
while True:
    connection, _ = listener.accept()
    print("Connection accepted for session [test-session]", flush=True)
    connection.close()
"""


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


@pytest.fixture
def fake_aws(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Put the stand-in ``aws`` first on PATH; returns its marker directory."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    script = bin_dir / "aws"
    script.write_text(f"#!{sys.executable}\n{_FAKE_AWS}", encoding="utf-8")
    script.chmod(script.stat().st_mode | stat.S_IXUSR)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}")
    monkeypatch.setenv("FAKE_SSM_DIR", str(tmp_path))
    return tmp_path


@pytest.fixture
def started() -> Iterator[list[subprocess.Popen[bytes]]]:
    """Tunnels a test started; any still running are stopped afterwards."""
    procs: list[subprocess.Popen[bytes]] = []
    yield procs
    for proc in procs:
        if proc.poll() is None:
            ssm_tunnel.stop_api_tunnel(proc)


def _start(port: int, **kwargs: Any) -> subprocess.Popen[bytes]:
    return ssm_tunnel.start_api_tunnel(
        "i-0123456789abcdef0", _ENDPOINT, port, "us-east-1", ready_poll_seconds=0.05, **kwargs
    )


def _connects(port: int) -> bool:
    try:
        socket.create_connection(("127.0.0.1", port), timeout=1).close()
    except OSError:
        return False
    return True


class TestListenerOwnership:
    def test_ready_once_the_plugin_reports_its_own_listener(
        self, fake_aws: Path, started: list[subprocess.Popen[bytes]]
    ) -> None:
        port = _free_port()
        proc = _start(port)
        started.append(proc)
        assert proc.poll() is None
        assert _connects(port)
        stdout, _stderr = ssm_tunnel.stop_api_tunnel(proc)
        assert f"Port {port} opened for sessionId test-session.".encode() in stdout
        assert proc.poll() is not None
        assert not _connects(port)

    def test_a_port_another_process_listens_on_is_refused_before_launch(
        self, fake_aws: Path
    ) -> None:
        with socket.socket() as foreign:
            foreign.bind(("127.0.0.1", 0))
            foreign.listen()
            port = foreign.getsockname()[1]
            with pytest.raises(
                RuntimeError, match=rf"Another process already listens on 127\.0\.0\.1:{port}"
            ):
                _start(port)
        assert not (fake_aws / "started").exists()

    def test_a_listener_taken_after_the_check_fails_the_start(
        self, fake_aws: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Another process binds the port between the check and the plugin's
        listen. The old rule ("the port accepts a connection") reported that
        listener as the tunnel; the plugin's own report never comes."""
        accepts = ssm_tunnel._local_port_accepts
        checks: list[int] = []

        def first_check_misses(port: int, timeout: float) -> bool:
            checks.append(port)
            return False if len(checks) == 1 else accepts(port, timeout)

        monkeypatch.setattr(ssm_tunnel, "_local_port_accepts", first_check_misses)
        with socket.socket() as foreign:
            foreign.bind(("127.0.0.1", 0))
            foreign.listen()
            port = foreign.getsockname()[1]
            with pytest.raises(RuntimeError, match=r"failed to start.*exit code 1"):
                _start(port, ready_wait_seconds=10)
        assert (fake_aws / "started").exists()

    def test_a_listener_the_plugin_never_reports_is_not_ready(
        self, fake_aws: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("FAKE_SSM_MODE", "unannounced")
        port = _free_port()
        with pytest.raises(RuntimeError, match="did not open its listener") as excinfo:
            _start(port, ready_wait_seconds=1.0)
        assert "has not reported its listener yet" in str(excinfo.value)
        pid = int((fake_aws / "started").read_text())
        with pytest.raises(ProcessLookupError):
            os.kill(pid, 0)


class TestOutputDraining:
    def test_a_chatty_session_never_blocks_on_its_pipes(
        self,
        fake_aws: Path,
        monkeypatch: pytest.MonkeyPatch,
        started: list[subprocess.Popen[bytes]],
    ) -> None:
        """4 MiB per stream is far past a pipe's buffer: an unread pipe stops the
        plugin at its next write, which stalls the tunnel."""
        monkeypatch.setenv("FAKE_SSM_MODE", "chatty")
        port = _free_port()
        proc = _start(port)
        started.append(proc)
        deadline = time.monotonic() + 20
        while not (fake_aws / "chatter-done").exists():
            assert time.monotonic() < deadline, "the stand-in blocked writing its output"
            time.sleep(0.05)
        assert _connects(port)
        stdout, stderr = ssm_tunnel.stop_api_tunnel(proc)
        assert 0 < len(stdout) <= ssm_tunnel._OUTPUT_TAIL_BYTES
        assert 0 < len(stderr) <= ssm_tunnel._OUTPUT_TAIL_BYTES
