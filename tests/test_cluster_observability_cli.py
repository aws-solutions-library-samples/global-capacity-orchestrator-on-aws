"""
Tests for the `gco monitoring` CLI (cli/commands/monitoring_cmd.py) plus the
port-forward / SSM-tunnel helpers it relies on.

The command layer is exercised through click's CliRunner with the AWS/kubectl
side mocked, so nothing touches a cluster. The pure argv builders
(build_port_forward_command, build_remote_host_port_forward_command) are tested
directly — they are the security-relevant seam (list form, validated inputs, no
shell) and the part that must be exactly right for the private-endpoint SSM
tunnel to work.
"""

from __future__ import annotations

import io
import json
import re
import socket
from typing import Any

import pytest
from click.testing import CliRunner

# The monitoring open tests patch the modules the CLI code holds
# (monitoring_cmd.cluster_ui, cluster_tunnel.ssm_tunnel) rather than dotted
# paths: see the note in tests/test_cluster_tunnel.py.
import cli.cluster_tunnel as cluster_tunnel
from cli import ssm_tunnel
from cli.commands import monitoring_cmd
from cli.kubectl_helpers import build_port_forward_command, describe_cluster_access
from cli.main import cli


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


# ---------------------------------------------------------------------------
# kubectl port-forward argv builder
# ---------------------------------------------------------------------------


class TestPortForwardCommand:
    def test_basic_command(self) -> None:
        cmd = build_port_forward_command(
            "monitoring", "svc/kube-prometheus-stack-grafana", 3000, 80
        )
        assert cmd == [
            "kubectl",
            "port-forward",
            "-n",
            "monitoring",
            "svc/kube-prometheus-stack-grafana",
            "3000:80",
        ]

    def test_server_and_tls_override_for_ssm(self) -> None:
        cmd = build_port_forward_command(
            "monitoring",
            "svc/kube-prometheus-stack-grafana",
            3000,
            80,
            server="https://127.0.0.1:8443",
            tls_server_name="ABC123.gr7.us-east-1.eks.amazonaws.com",
        )
        assert "--server" in cmd and "https://127.0.0.1:8443" in cmd
        assert "--tls-server-name" in cmd
        assert "ABC123.gr7.us-east-1.eks.amazonaws.com" in cmd

    def test_rejects_bad_namespace(self) -> None:
        with pytest.raises(ValueError, match=re.escape("Invalid namespace 'Bad NS'")):
            build_port_forward_command("Bad NS", "svc/x", 3000, 80)

    def test_rejects_bad_target(self) -> None:
        with pytest.raises(ValueError, match=re.escape("Invalid port-forward target 'grafana'")):
            build_port_forward_command("monitoring", "grafana", 3000, 80)

    def test_rejects_non_https_server(self) -> None:
        with pytest.raises(
            ValueError,
            match=re.escape("Invalid --server 'http://localhost:8443': must start with https://"),
        ):
            build_port_forward_command(
                "monitoring", "svc/x", 3000, 80, server="http://localhost:8443"
            )

    def test_rejects_bad_tls_server_name(self) -> None:
        with pytest.raises(ValueError, match="--tls-server-name"):
            build_port_forward_command(
                "monitoring",
                "svc/x",
                3000,
                80,
                server="https://127.0.0.1:8443",
                tls_server_name="not a valid hostname!",
            )

    @pytest.mark.parametrize("port", [0, 70000, "abc", -1])
    def test_rejects_bad_ports(self, port: object) -> None:
        with pytest.raises(ValueError, match=re.escape("Invalid local port ")):
            build_port_forward_command("monitoring", "svc/x", port, 80)

    @pytest.mark.parametrize(
        ("field", "message"),
        [
            ("namespace", "Invalid namespace"),
            ("target", "Invalid port-forward target"),
            ("tls_server_name", "Invalid --tls-server-name"),
        ],
    )
    def test_rejects_a_trailing_newline(self, field: str, message: str) -> None:
        # ``$`` also matches just before a final newline, so each check is a fullmatch.
        values: dict[str, Any] = {
            "namespace": "monitoring",
            "target": "svc/x",
            "server": "https://127.0.0.1:8443",
            "tls_server_name": "h.example.com",
        }
        assert build_port_forward_command(local_port=3000, remote_port=80, **values)
        values[field] += "\n"
        with pytest.raises(ValueError, match=re.escape(message)):
            build_port_forward_command(local_port=3000, remote_port=80, **values)


# ---------------------------------------------------------------------------
# SSM remote-host tunnel argv builder
# ---------------------------------------------------------------------------


class TestSsmTunnelCommand:
    def test_builds_documented_parameters(self) -> None:
        cmd = ssm_tunnel.build_remote_host_port_forward_command(
            "i-0123456789abcdef0",
            "ABC123.gr7.us-east-1.eks.amazonaws.com",
            8443,
            "us-east-1",
        )
        assert cmd[:7] == [
            "aws",
            "ssm",
            "start-session",
            "--target",
            "i-0123456789abcdef0",
            "--region",
            "us-east-1",
        ]
        assert "--document-name" in cmd
        assert "AWS-StartPortForwardingSessionToRemoteHost" in cmd
        params = json.loads(cmd[cmd.index("--parameters") + 1])
        assert params == {
            "host": ["ABC123.gr7.us-east-1.eks.amazonaws.com"],
            "portNumber": ["443"],
            "localPortNumber": ["8443"],
        }

    def test_endpoint_host_parses_url(self) -> None:
        assert (
            ssm_tunnel.endpoint_host("https://ABC.gr7.us-east-1.eks.amazonaws.com")
            == "ABC.gr7.us-east-1.eks.amazonaws.com"
        )
        # bare host passes through
        assert ssm_tunnel.endpoint_host("ABC.example.com") == "ABC.example.com"

    def test_rejects_bad_instance_id(self) -> None:
        with pytest.raises(ValueError, match=re.escape("Invalid SSM target 'not-an-instance'")):
            ssm_tunnel.build_remote_host_port_forward_command(
                "not-an-instance", "h.example.com", 8443, "us-east-1"
            )

    def test_rejects_bad_region(self) -> None:
        with pytest.raises(ValueError, match=re.escape("Invalid AWS region 'not_a_region'")):
            ssm_tunnel.build_remote_host_port_forward_command(
                "i-0123456789abcdef0", "h.example.com", 8443, "not_a_region"
            )

    def test_rejects_bad_host(self) -> None:
        with pytest.raises(ValueError, match=re.escape("Invalid remote host 'bad host!'")):
            ssm_tunnel.build_remote_host_port_forward_command(
                "i-0123456789abcdef0", "bad host!", 8443, "us-east-1"
            )

    @pytest.mark.parametrize("port", [0, 70000, "nope"])
    def test_rejects_bad_local_port(self, port: object) -> None:
        with pytest.raises(ValueError, match=re.escape("Invalid local port ")):
            ssm_tunnel.build_remote_host_port_forward_command(
                "i-0123456789abcdef0", "h.example.com", port, "us-east-1"
            )

    @pytest.mark.parametrize(
        ("field", "message"),
        [
            ("instance_id", "Invalid SSM target"),
            ("remote_host", "Invalid remote host"),
            ("region", "Invalid AWS region"),
        ],
    )
    def test_rejects_a_trailing_newline(self, field: str, message: str) -> None:
        # ``$`` also matches just before a final newline, so each check is a fullmatch.
        values: dict[str, Any] = {
            "instance_id": "i-0123456789abcdef0",
            "remote_host": "h.example.com",
            "region": "us-east-1",
        }
        assert ssm_tunnel.build_remote_host_port_forward_command(local_port=8443, **values)
        values[field] += "\n"
        with pytest.raises(ValueError, match=re.escape(message)):
            ssm_tunnel.build_remote_host_port_forward_command(local_port=8443, **values)

    def test_endpoint_host_rejects_empty(self) -> None:
        with pytest.raises(
            ValueError, match=re.escape("Could not parse a hostname from endpoint 'https://'")
        ):
            ssm_tunnel.endpoint_host("https://")


# ---------------------------------------------------------------------------
# gco monitoring enable / disable / status
# ---------------------------------------------------------------------------


class TestToggleCommands:
    def test_status_prints_config(self, runner: CliRunner, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            "cli.stacks.get_cluster_observability_config",
            lambda: {"enabled": True, "grafana": {"admin_user": "admin"}},
        )
        result = runner.invoke(cli, ["monitoring", "status"])
        assert result.exit_code == 0, result.output

    def test_enable_flips_toggle(self, runner: CliRunner, monkeypatch: pytest.MonkeyPatch) -> None:
        captured: dict[str, object] = {}
        monkeypatch.setattr(
            "cli.stacks.update_cluster_observability_config",
            lambda settings: captured.update(settings),
        )
        result = runner.invoke(cli, ["monitoring", "enable", "-y"])
        assert result.exit_code == 0, result.output
        assert captured == {"enabled": True}

    def test_disable_flips_toggle(self, runner: CliRunner, monkeypatch: pytest.MonkeyPatch) -> None:
        captured: dict[str, object] = {}
        monkeypatch.setattr(
            "cli.stacks.update_cluster_observability_config",
            lambda settings: captured.update(settings),
        )
        result = runner.invoke(cli, ["monitoring", "disable", "-y"])
        assert result.exit_code == 0, result.output
        assert captured == {"enabled": False}

    def test_disable_confirm_prompt_accepts(
        self, runner: CliRunner, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        captured: dict[str, object] = {}
        monkeypatch.setattr(
            "cli.stacks.update_cluster_observability_config",
            lambda settings: captured.update(settings),
        )
        result = runner.invoke(cli, ["monitoring", "disable"], input="y\n")
        assert result.exit_code == 0, result.output
        assert captured == {"enabled": False}


# ---------------------------------------------------------------------------
# gco monitoring open
# ---------------------------------------------------------------------------


class TestOpenCommand:
    def _patch_common(self, monkeypatch: pytest.MonkeyPatch, captured: dict[str, object]) -> None:
        monkeypatch.setattr("cli.kubectl_helpers.update_kubeconfig", lambda c, r: None)
        monkeypatch.setattr(
            monitoring_cmd.cluster_ui, "exec_port_forward", _recording_forward(captured)
        )

    def test_open_public_endpoint_direct_forward(
        self, runner: CliRunner, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        captured: dict[str, object] = {}
        self._patch_common(monkeypatch, captured)
        monkeypatch.setattr(
            "cli.kubectl_helpers.describe_cluster_access",
            lambda c, r: {"public": True, "endpoint": "https://x.eks.amazonaws.com"},
        )
        result = runner.invoke(cli, ["monitoring", "open", "--region", "us-east-1"])
        assert result.exit_code == 0, result.output
        # Direct forward: no --server override.
        assert "--server" not in captured["cmd"]
        assert "svc/kube-prometheus-stack-grafana" in captured["cmd"]
        assert not any(str(arg).startswith("--context") for arg in captured["cmd"])

    def test_open_forwards_through_the_clusters_context(
        self, runner: CliRunner, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        context = "arn:aws:eks:us-east-1:123456789012:cluster/gco-us-east-1"
        captured: dict[str, object] = {}
        self._patch_common(monkeypatch, captured)
        monkeypatch.setattr("cli.kubectl_helpers.update_kubeconfig", lambda c, r: context)
        monkeypatch.setattr(
            "cli.kubectl_helpers.describe_cluster_access",
            lambda c, r: {"public": True, "endpoint": "https://x.eks.amazonaws.com"},
        )
        result = runner.invoke(cli, ["monitoring", "open", "--region", "us-east-1"])
        assert result.exit_code == 0, result.output
        assert captured["cmd"][-1] == f"--context={context}"

    def test_open_private_without_ssm_warns_but_tries(
        self, runner: CliRunner, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        captured: dict[str, object] = {}
        self._patch_common(monkeypatch, captured)
        monkeypatch.setattr(
            "cli.kubectl_helpers.describe_cluster_access",
            lambda c, r: {"public": False, "endpoint": "https://x.eks.amazonaws.com"},
        )
        result = runner.invoke(cli, ["monitoring", "open", "--region", "us-east-1"])
        assert result.exit_code == 0, result.output
        assert "PRIVATE API endpoint" in result.output
        # Still attempts a direct forward (no tunnel server override).
        assert "--server" not in captured["cmd"]

    def test_open_private_with_ssm_tunnels(
        self, runner: CliRunner, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        captured: dict[str, object] = {}
        self._patch_common(monkeypatch, captured)
        monkeypatch.setattr(
            "cli.kubectl_helpers.describe_cluster_access",
            lambda c, r: {
                "public": False,
                "endpoint": "https://ABC.gr7.us-east-1.eks.amazonaws.com",
            },
        )

        class _FakeProc:
            stdout = None
            stderr = None

            def __init__(self) -> None:
                self.terminated = False

            def poll(self) -> int | None:
                return -15 if self.terminated else None

            def terminate(self) -> None:
                self.terminated = True
                captured["terminated"] = True

            def wait(self, timeout: float | None = None) -> int:
                captured["reaped"] = True
                return -15

        started: dict[str, object] = {}

        def _fake_start(instance, endpoint, local_port, region, **kw):
            started["instance"] = instance
            started["endpoint"] = endpoint
            started["local_port"] = local_port
            return _FakeProc()

        monkeypatch.setattr(cluster_tunnel.ssm_tunnel, "start_api_tunnel", _fake_start)

        result = runner.invoke(
            cli,
            ["monitoring", "open", "--region", "us-east-1", "--via-ssm", "i-0123456789abcdef0"],
        )
        assert result.exit_code == 0, result.output
        assert started["instance"] == "i-0123456789abcdef0"
        # kubectl points at the SSM local port with the real endpoint as TLS SNI.
        cmd = captured["cmd"]
        assert "--server" in cmd and "https://127.0.0.1:8443" in cmd
        assert "ABC.gr7.us-east-1.eks.amazonaws.com" in cmd
        # Tunnel is torn down after the forward returns.
        assert captured.get("terminated") is True
        assert captured.get("reaped") is True


def test_describe_cluster_access_parses_query(monkeypatch: pytest.MonkeyPatch) -> None:
    """describe_cluster_access shells out to the AWS CLI and parses the JSON."""
    import subprocess as _sp

    class _Result:
        returncode = 0
        stdout = json.dumps(
            {
                "endpoint": "https://x.eks.amazonaws.com",
                "public": False,
                "private": True,
                "publicCidrs": [],
            }
        )
        stderr = ""

    monkeypatch.setattr(_sp, "run", lambda *a, **k: _Result())
    access = describe_cluster_access("gco-us-east-1", "us-east-1")
    assert access["public"] is False
    assert access["private"] is True
    assert access["endpoint"] == "https://x.eks.amazonaws.com"


def test_describe_cluster_access_raises_on_cli_error(monkeypatch: pytest.MonkeyPatch) -> None:
    import subprocess as _sp

    class _Result:
        returncode = 254
        stdout = ""
        stderr = "AccessDenied"

    monkeypatch.setattr(_sp, "run", lambda *a, **k: _Result())
    with pytest.raises(RuntimeError, match="Failed to describe cluster"):
        describe_cluster_access("gco-us-east-1", "us-east-1")


def test_describe_cluster_access_missing_aws_cli(monkeypatch: pytest.MonkeyPatch) -> None:
    import subprocess as _sp

    def _boom(*a: object, **k: object) -> None:
        raise FileNotFoundError

    monkeypatch.setattr(_sp, "run", _boom)
    with pytest.raises(RuntimeError, match="AWS CLI not found"):
        describe_cluster_access("gco-us-east-1", "us-east-1")


class _TunnelProc:
    """A Popen stand-in: real byte streams to drain, a scripted exit."""

    def __init__(
        self,
        *,
        stdout: bytes = b"",
        stderr: bytes = b"",
        polls: list[int | None] | None = None,
        exit_code: int = -15,
        pid: int | None = None,
    ) -> None:
        self.stdout = io.BytesIO(stdout)
        self.stderr = io.BytesIO(stderr)
        self._polls = list(polls or [])
        self._exit_code = exit_code
        self.returncode: int | None = None
        self.events: list[str] = []
        if pid is not None:
            self.pid = pid

    def poll(self) -> int | None:
        if self.returncode is not None:
            return self.returncode
        return self._polls.pop(0) if self._polls else None

    def terminate(self) -> None:
        self.events.append("terminate")
        self.returncode = self._exit_code

    def kill(self) -> None:
        self.events.append("kill")
        self.returncode = -9

    def wait(self, timeout: float | None = None) -> int:
        self.events.append("wait")
        if self.returncode is None:
            self.returncode = self._exit_code
        return self.returncode


def _opened(port: int = 8443) -> bytes:
    return f"Starting session with SessionId: s-1\nPort {port} opened for sessionId s-1.\n".encode()


class TestStartApiTunnel:
    @staticmethod
    def _accepts(monkeypatch: pytest.MonkeyPatch, answers: list[bool]) -> list[int]:
        """Script ``_local_port_accepts``: the first answer is the pre-launch check."""
        asked: list[int] = []

        def accepts(port: int, timeout: float) -> bool:
            asked.append(port)
            return answers.pop(0) if answers else True

        monkeypatch.setattr(ssm_tunnel, "_local_port_accepts", accepts)
        return asked

    def test_returns_proc_once_its_listener_is_reported_and_accepts(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        proc = _TunnelProc(stdout=_opened())
        popen_call: dict[str, object] = {}

        def popen(*args: object, **kwargs: object) -> _TunnelProc:
            popen_call["args"] = args
            popen_call["kwargs"] = kwargs
            return proc

        asked = self._accepts(monkeypatch, [False, True])
        monkeypatch.setattr(ssm_tunnel.subprocess, "Popen", popen)
        assert (
            ssm_tunnel.start_api_tunnel(
                "i-0123456789abcdef0",
                "https://ABC.gr7.us-east-1.eks.amazonaws.com",
                8443,
                "us-east-1",
                ready_poll_seconds=0.01,
            )
            is proc
        )
        assert asked == [8443, 8443]
        kwargs = popen_call["kwargs"]
        assert isinstance(kwargs, dict)
        assert kwargs["stdout"] is kwargs["stderr"] is ssm_tunnel.subprocess.PIPE
        assert kwargs["start_new_session"] is (ssm_tunnel.os.name == "posix")
        if ssm_tunnel.os.name == "nt":  # pragma: no cover - POSIX CI
            assert kwargs["creationflags"] == getattr(
                ssm_tunnel.subprocess, "CREATE_NEW_PROCESS_GROUP", 0
            )

    def test_windows_launch_uses_new_process_group(self, monkeypatch: pytest.MonkeyPatch) -> None:
        captured: dict[str, object] = {}

        def popen(*args: object, **kwargs: object) -> _TunnelProc:
            captured.update(kwargs)
            return _TunnelProc(stdout=_opened())

        self._accepts(monkeypatch, [False, True])
        monkeypatch.setattr(ssm_tunnel.os, "name", "nt")
        monkeypatch.setattr(
            ssm_tunnel.subprocess,
            "CREATE_NEW_PROCESS_GROUP",
            512,
            raising=False,
        )
        monkeypatch.setattr(ssm_tunnel.subprocess, "Popen", popen)
        ssm_tunnel.start_api_tunnel(
            "i-0123456789abcdef0",
            "https://ABC.gr7.us-east-1.eks.amazonaws.com",
            8443,
            "us-east-1",
            ready_poll_seconds=0.01,
        )
        assert captured["start_new_session"] is False
        assert captured["creationflags"] == 512

    def test_a_port_already_in_use_is_refused_before_launch(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def popen(*args: object, **kwargs: object) -> None:
            raise AssertionError("nothing may start on a port another process holds")

        self._accepts(monkeypatch, [True])
        monkeypatch.setattr(ssm_tunnel.subprocess, "Popen", popen)
        with pytest.raises(RuntimeError, match=r"already listens on 127\.0\.0\.1:8443"):
            ssm_tunnel.start_api_tunnel("i-0123456789abcdef0", "h.example.com", 8443, "us-east-1")

    def test_a_listener_reported_for_another_port_does_not_count(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        proc = _TunnelProc(stdout=_opened(9999))
        clock = iter([0.0, 0.0, 99.0])
        self._accepts(monkeypatch, [False])
        monkeypatch.setattr(ssm_tunnel.subprocess, "Popen", lambda *a, **k: proc)
        monkeypatch.setattr(ssm_tunnel.time, "monotonic", lambda: next(clock, 99.0))
        monkeypatch.setattr(ssm_tunnel.time, "sleep", lambda _seconds: None)
        with pytest.raises(RuntimeError, match="has not reported its listener yet"):
            ssm_tunnel.start_api_tunnel(
                "i-0123456789abcdef0", "h.example.com", 8443, "us-east-1", ready_wait_seconds=1
            )
        assert "terminate" in proc.events

    def test_waits_until_the_reported_listener_accepts(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        sleeps: list[float] = []
        asked = self._accepts(monkeypatch, [False, False, False, True])
        monkeypatch.setattr(
            ssm_tunnel.subprocess, "Popen", lambda *a, **k: _TunnelProc(stdout=_opened())
        )
        monkeypatch.setattr(ssm_tunnel.time, "sleep", sleeps.append)
        ssm_tunnel.start_api_tunnel(
            "i-0123456789abcdef0",
            "https://ABC.gr7.us-east-1.eks.amazonaws.com",
            8443,
            "us-east-1",
            ready_wait_seconds=5,
            ready_poll_seconds=0.1,
        )
        assert len(asked) == 4
        assert sleeps and all(pause == pytest.approx(0.1) for pause in sleeps[-2:])

    def test_reports_session_exit_while_waiting(self, monkeypatch: pytest.MonkeyPatch) -> None:
        proc = _TunnelProc(stderr=b"Session Manager channel failed", polls=[None, 23])
        self._accepts(monkeypatch, [False])
        monkeypatch.setattr(ssm_tunnel.subprocess, "Popen", lambda *a, **k: proc)
        monkeypatch.setattr(ssm_tunnel.time, "sleep", lambda _seconds: None)
        with pytest.raises(RuntimeError, match=r"exit code 23.*Session Manager channel failed"):
            ssm_tunnel.start_api_tunnel(
                "i-0123456789abcdef0",
                "https://ABC.gr7.us-east-1.eks.amazonaws.com",
                8443,
                "us-east-1",
                ready_wait_seconds=5,
            )

    def test_a_session_exit_after_the_listener_accepts_is_reported(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        proc = _TunnelProc(stdout=_opened(), stderr=b"session closed", polls=[None, 17])
        self._accepts(monkeypatch, [False, True])
        monkeypatch.setattr(ssm_tunnel.subprocess, "Popen", lambda *a, **k: proc)
        monkeypatch.setattr(ssm_tunnel.time, "sleep", lambda _seconds: None)
        with pytest.raises(RuntimeError, match="exited during readiness: exit code 17"):
            ssm_tunnel.start_api_tunnel(
                "i-0123456789abcdef0", "h.example.com", 8443, "us-east-1", ready_wait_seconds=5
            )

    def test_timeout_terminates_and_reaps_process(self, monkeypatch: pytest.MonkeyPatch) -> None:
        proc = _TunnelProc(stdout=_opened(), stderr=b"listener setup failed")
        clock = iter((0.0, 2.0))
        self._accepts(monkeypatch, [False, False])
        monkeypatch.setattr(ssm_tunnel.subprocess, "Popen", lambda *a, **k: proc)
        monkeypatch.setattr(ssm_tunnel.time, "monotonic", lambda: next(clock, 2.0))
        with pytest.raises(RuntimeError, match="did not open its listener") as exc_info:
            ssm_tunnel.start_api_tunnel(
                "i-0123456789abcdef0",
                "https://ABC.gr7.us-east-1.eks.amazonaws.com",
                8443,
                "us-east-1",
                ready_wait_seconds=1,
            )
        assert "the reported listener did not accept a connection" in str(exc_info.value)
        assert "listener setup failed" in str(exc_info.value)
        assert proc.events[:2] == ["terminate", "wait"]

    def test_interrupted_startup_terminates_and_reaps_process(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        proc = _TunnelProc()

        def interrupt(_seconds: float) -> None:
            raise KeyboardInterrupt

        self._accepts(monkeypatch, [False])
        monkeypatch.setattr(ssm_tunnel.subprocess, "Popen", lambda *a, **k: proc)
        monkeypatch.setattr(ssm_tunnel.time, "sleep", interrupt)
        with pytest.raises(KeyboardInterrupt):
            ssm_tunnel.start_api_tunnel(
                "i-0123456789abcdef0",
                "https://ABC.gr7.us-east-1.eks.amazonaws.com",
                8443,
                "us-east-1",
            )
        assert proc.events[:2] == ["terminate", "wait"]

    def test_raises_when_session_dies_immediately(self, monkeypatch: pytest.MonkeyPatch) -> None:
        proc = _TunnelProc(stderr=b"SessionManagerPlugin not found", polls=[1])
        self._accepts(monkeypatch, [False])
        monkeypatch.setattr(ssm_tunnel.subprocess, "Popen", lambda *a, **k: proc)
        with pytest.raises(RuntimeError, match="SSM port-forwarding session failed"):
            ssm_tunnel.start_api_tunnel(
                "i-0123456789abcdef0",
                "https://ABC.gr7.us-east-1.eks.amazonaws.com",
                8443,
                "us-east-1",
                ready_wait_seconds=0,
            )

    def test_raises_when_aws_cli_missing(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def _boom(*a: object, **k: object) -> None:
            raise FileNotFoundError

        self._accepts(monkeypatch, [False])
        monkeypatch.setattr(ssm_tunnel.subprocess, "Popen", _boom)
        with pytest.raises(RuntimeError, match="AWS CLI not found"):
            ssm_tunnel.start_api_tunnel(
                "i-0123456789abcdef0", "h.example.com", 8443, "us-east-1", ready_wait_seconds=0
            )

    def test_local_port_probe_against_a_real_socket(self) -> None:
        with socket.socket() as server:
            server.bind(("127.0.0.1", 0))
            server.listen()
            port = server.getsockname()[1]
            assert ssm_tunnel._local_port_accepts(port, 1.0) is True
        assert ssm_tunnel._local_port_accepts(port, 1.0) is False


class TestStopApiTunnel:
    def test_escalates_complete_posix_process_group(self, monkeypatch: pytest.MonkeyPatch) -> None:
        class _Stubborn(_TunnelProc):
            """Ignores the graceful signal; dies to the forced one."""

            def wait(self, timeout: float | None = None) -> int:
                self.events.append("wait")
                if self.returncode is None:
                    raise ssm_tunnel.subprocess.TimeoutExpired("aws", timeout or 0)
                return self.returncode

        proc = _Stubborn(stdout=b"stdout", stderr=b"stderr", pid=4321)
        signals: list[tuple[int, int]] = []

        def killpg(pid: int, sent_signal: int) -> None:
            signals.append((pid, sent_signal))
            if sent_signal == ssm_tunnel.signal.SIGKILL:
                proc.returncode = -9

        monkeypatch.setattr(ssm_tunnel.os, "name", "posix")
        monkeypatch.setattr(ssm_tunnel.os, "killpg", killpg)
        assert ssm_tunnel.stop_api_tunnel(proc, wait_seconds=1) == (b"stdout", b"stderr")
        assert signals == [
            (4321, ssm_tunnel.signal.SIGTERM),
            (4321, ssm_tunnel.signal.SIGKILL),
        ]

    def test_forces_windows_tree_before_wrapper_fallback(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        class _Stubborn(_TunnelProc):
            def wait(self, timeout: float | None = None) -> int:
                self.events.append("wait")
                if len(commands) < 2:
                    raise ssm_tunnel.subprocess.TimeoutExpired("aws", timeout or 0)
                return 1

        class _Result:
            def __init__(self, returncode: int) -> None:
                self.returncode = returncode

        commands: list[list[str]] = []

        def taskkill(command: list[str], **kwargs: object) -> _Result:
            commands.append(command)
            return _Result(1 if len(commands) == 1 else 0)

        proc = _Stubborn(pid=4321)
        monkeypatch.setattr(ssm_tunnel.os, "name", "nt")
        monkeypatch.setattr(ssm_tunnel.shutil, "which", lambda _name: "taskkill.exe")
        monkeypatch.setattr(ssm_tunnel.subprocess, "run", taskkill)
        ssm_tunnel.stop_api_tunnel(proc, wait_seconds=1)
        assert commands == [
            ["taskkill.exe", "/PID", "4321", "/T"],
            ["taskkill.exe", "/PID", "4321", "/T", "/F"],
        ]
        assert "terminate" not in proc.events and "kill" not in proc.events

    def test_rejects_a_non_positive_wait(self) -> None:
        with pytest.raises(ValueError, match="wait_seconds must be positive"):
            ssm_tunnel.stop_api_tunnel(_TunnelProc(), wait_seconds=0)


class TestOpenErrorPaths:
    def test_open_update_kubeconfig_failure_exits(
        self, runner: CliRunner, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def _boom(cluster: str, region: str) -> None:
            raise RuntimeError("kubeconfig failed")

        monkeypatch.setattr("cli.kubectl_helpers.update_kubeconfig", _boom)
        result = runner.invoke(cli, ["monitoring", "open", "--region", "us-east-1"])
        assert result.exit_code == 1
        assert "kubeconfig failed" in result.output

    def test_open_describe_failure_falls_back_to_direct(
        self, runner: CliRunner, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        captured: dict[str, object] = {}
        monkeypatch.setattr("cli.kubectl_helpers.update_kubeconfig", lambda c, r: None)
        monkeypatch.setattr(
            monitoring_cmd.cluster_ui, "exec_port_forward", _recording_forward(captured)
        )

        def _boom(cluster: str, region: str) -> dict[str, object]:
            raise RuntimeError("describe failed")

        monkeypatch.setattr("cli.kubectl_helpers.describe_cluster_access", _boom)
        result = runner.invoke(cli, ["monitoring", "open", "--region", "us-east-1"])
        assert result.exit_code == 0, result.output
        assert "Could not determine endpoint access mode" in result.output
        assert "--server" not in captured["cmd"]

    def test_open_ssm_failure_exits(
        self, runner: CliRunner, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr("cli.kubectl_helpers.update_kubeconfig", lambda c, r: None)
        monkeypatch.setattr(
            "cli.kubectl_helpers.describe_cluster_access",
            lambda c, r: {"public": False, "endpoint": "https://x.eks.amazonaws.com"},
        )

        def _boom(*a: object, **k: object) -> None:
            raise RuntimeError("tunnel failed")

        monkeypatch.setattr(cluster_tunnel.ssm_tunnel, "start_api_tunnel", _boom)
        result = runner.invoke(
            cli,
            ["monitoring", "open", "--region", "us-east-1", "--via-ssm", "i-0123456789abcdef0"],
        )
        assert result.exit_code == 1
        assert "tunnel failed" in result.output


class TestToggleErrorPaths:
    def test_status_error_exits(self, runner: CliRunner, monkeypatch: pytest.MonkeyPatch) -> None:
        def _boom() -> dict[str, object]:
            raise RuntimeError("cdk.json not found")

        monkeypatch.setattr("cli.stacks.get_cluster_observability_config", _boom)
        result = runner.invoke(cli, ["monitoring", "status"])
        assert result.exit_code == 1
        assert "Failed to read" in result.output

    def test_enable_error_exits(self, runner: CliRunner, monkeypatch: pytest.MonkeyPatch) -> None:
        def _boom(settings: dict[str, object]) -> None:
            raise RuntimeError("write failed")

        monkeypatch.setattr("cli.stacks.update_cluster_observability_config", _boom)
        result = runner.invoke(cli, ["monitoring", "enable", "-y"])
        assert result.exit_code == 1
        assert "Failed to enable" in result.output

    def test_disable_error_exits(self, runner: CliRunner, monkeypatch: pytest.MonkeyPatch) -> None:
        def _boom(settings: dict[str, object]) -> None:
            raise RuntimeError("write failed")

        monkeypatch.setattr("cli.stacks.update_cluster_observability_config", _boom)
        result = runner.invoke(cli, ["monitoring", "disable", "-y"])
        assert result.exit_code == 1
        assert "Failed to disable" in result.output

    def test_enable_confirm_prompt_accepts(
        self, runner: CliRunner, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        captured: dict[str, object] = {}
        monkeypatch.setattr(
            "cli.stacks.update_cluster_observability_config",
            lambda settings: captured.update(settings),
        )
        # No -y: the confirmation prompt fires; answer "y".
        result = runner.invoke(cli, ["monitoring", "enable"], input="y\n")
        assert result.exit_code == 0, result.output
        assert captured == {"enabled": True}


def _recording_forward(captured: dict[str, object]) -> Any:
    """Stand-in for cli.cluster_ui.exec_port_forward: record, then report kubectl ready."""

    def forward(cmd: list[str], local_port: int, *, on_ready: Any = None, **_: Any) -> None:
        captured["cmd"] = cmd
        captured["port"] = local_port
        if on_ready is not None:
            on_ready()

    return forward


def test_open_announces_the_url_only_once_kubectl_listens(
    runner: CliRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The forwarding line comes from on_ready, so a forward that fails never prints it."""
    monkeypatch.setattr("cli.kubectl_helpers.update_kubeconfig", lambda c, r: None)
    monkeypatch.setattr(
        "cli.kubectl_helpers.describe_cluster_access",
        lambda c, r: {"public": True, "private": False, "endpoint": "https://x.eks.amazonaws.com"},
    )

    def never_listens(cmd: list[str], local_port: int, **_: Any) -> None:
        raise RuntimeError(
            f"kubectl port-forward exited with code 1 before localhost:{local_port} was listening"
        )

    monkeypatch.setattr(monitoring_cmd.cluster_ui, "exec_port_forward", never_listens)
    result = runner.invoke(cli, ["monitoring", "open", "--region", "us-east-1"])
    assert result.exit_code == 1
    assert "Starting kubectl port-forward for grafana on localhost:3000" in result.output
    assert "before localhost:3000 was listening" in result.output
    assert "Forwarding grafana" not in result.output
    assert "Log in with the Grafana admin credential" not in result.output


def test_open_resolves_region_from_cdk_json(
    runner: CliRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With no --region, open uses the first cdk.json regional entry."""
    captured: dict[str, object] = {}
    monkeypatch.setattr(
        "cli.kubectl_helpers.update_kubeconfig", lambda c, r: captured.update(cluster=c, region=r)
    )
    monkeypatch.setattr(
        "cli.kubectl_helpers.describe_cluster_access",
        lambda c, r: {"public": True, "endpoint": ""},
    )
    monkeypatch.setattr(
        monitoring_cmd.cluster_ui, "exec_port_forward", _recording_forward(captured)
    )
    monkeypatch.setattr("cli.config._load_cdk_json", lambda: {"regional": ["us-west-2"]})
    result = runner.invoke(cli, ["monitoring", "open", "--service", "prometheus"])
    assert result.exit_code == 0, result.output
    assert captured["region"] == "us-west-2"
    assert captured["cluster"].endswith("-us-west-2")
    assert "svc/kube-prometheus-stack-prometheus" in captured["cmd"]
