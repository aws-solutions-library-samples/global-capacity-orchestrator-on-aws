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

import json

import pytest
from click.testing import CliRunner

from cli import ssm_tunnel
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
        with pytest.raises(ValueError):
            build_port_forward_command("Bad NS", "svc/x", 3000, 80)

    def test_rejects_bad_target(self) -> None:
        with pytest.raises(ValueError):
            build_port_forward_command("monitoring", "grafana", 3000, 80)

    def test_rejects_non_https_server(self) -> None:
        with pytest.raises(ValueError):
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
        with pytest.raises(ValueError):
            build_port_forward_command("monitoring", "svc/x", port, 80)


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
        with pytest.raises(ValueError):
            ssm_tunnel.build_remote_host_port_forward_command(
                "not-an-instance", "h.example.com", 8443, "us-east-1"
            )

    def test_rejects_bad_region(self) -> None:
        with pytest.raises(ValueError):
            ssm_tunnel.build_remote_host_port_forward_command(
                "i-0123456789abcdef0", "h.example.com", 8443, "not_a_region"
            )

    def test_rejects_bad_host(self) -> None:
        with pytest.raises(ValueError):
            ssm_tunnel.build_remote_host_port_forward_command(
                "i-0123456789abcdef0", "bad host!", 8443, "us-east-1"
            )

    @pytest.mark.parametrize("port", [0, 70000, "nope"])
    def test_rejects_bad_local_port(self, port: object) -> None:
        with pytest.raises(ValueError):
            ssm_tunnel.build_remote_host_port_forward_command(
                "i-0123456789abcdef0", "h.example.com", port, "us-east-1"
            )

    def test_endpoint_host_rejects_empty(self) -> None:
        with pytest.raises(ValueError):
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
            "cli.commands.monitoring_cmd._exec_port_forward",
            lambda cmd: captured.__setitem__("cmd", cmd),
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
            def __init__(self) -> None:
                self.terminated = False

            def poll(self) -> int | None:
                return -15 if self.terminated else None

            def terminate(self) -> None:
                self.terminated = True
                captured["terminated"] = True

            def communicate(self, timeout: float | None = None) -> tuple[bytes, bytes]:
                captured["reaped"] = True
                return b"", b""

        started: dict[str, object] = {}

        def _fake_start(instance, endpoint, local_port, region, **kw):
            started["instance"] = instance
            started["endpoint"] = endpoint
            started["local_port"] = local_port
            return _FakeProc()

        monkeypatch.setattr("cli.ssm_tunnel.start_api_tunnel", _fake_start)

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


class TestStartApiTunnel:
    class _Socket:
        def __init__(self) -> None:
            self.closed = False

        def close(self) -> None:
            self.closed = True

    def test_returns_proc_only_after_listener_accepts(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        class _Proc:
            def poll(self) -> None:
                return None

        connection = self._Socket()
        popen_call: dict[str, object] = {}

        def popen(*args: object, **kwargs: object) -> _Proc:
            popen_call["args"] = args
            popen_call["kwargs"] = kwargs
            return _Proc()

        monkeypatch.setattr(ssm_tunnel.subprocess, "Popen", popen)
        monkeypatch.setattr(
            ssm_tunnel.socket,
            "create_connection",
            lambda *a, **k: connection,
        )
        proc = ssm_tunnel.start_api_tunnel(
            "i-0123456789abcdef0",
            "https://ABC.gr7.us-east-1.eks.amazonaws.com",
            8443,
            "us-east-1",
            ready_wait_seconds=0,
        )
        assert isinstance(proc, _Proc)
        assert connection.closed is True
        kwargs = popen_call["kwargs"]
        assert isinstance(kwargs, dict)
        assert kwargs["start_new_session"] is (ssm_tunnel.os.name == "posix")
        if ssm_tunnel.os.name == "nt":
            assert kwargs["creationflags"] == getattr(
                ssm_tunnel.subprocess, "CREATE_NEW_PROCESS_GROUP", 0
            )

    def test_windows_launch_uses_new_process_group(self, monkeypatch: pytest.MonkeyPatch) -> None:
        class _Proc:
            @staticmethod
            def poll() -> None:
                return None

        captured: dict[str, object] = {}

        def popen(*args: object, **kwargs: object) -> _Proc:
            captured.update(kwargs)
            return _Proc()

        monkeypatch.setattr(ssm_tunnel.os, "name", "nt")
        monkeypatch.setattr(
            ssm_tunnel.subprocess,
            "CREATE_NEW_PROCESS_GROUP",
            512,
            raising=False,
        )
        monkeypatch.setattr(ssm_tunnel.subprocess, "Popen", popen)
        monkeypatch.setattr(
            ssm_tunnel.socket,
            "create_connection",
            lambda *a, **k: self._Socket(),
        )

        ssm_tunnel.start_api_tunnel(
            "i-0123456789abcdef0",
            "https://ABC.gr7.us-east-1.eks.amazonaws.com",
            8443,
            "us-east-1",
            ready_wait_seconds=0,
        )

        assert captured["start_new_session"] is False
        assert captured["creationflags"] == 512

    def test_waits_for_delayed_listener(self, monkeypatch: pytest.MonkeyPatch) -> None:
        class _Proc:
            def poll(self) -> None:
                return None

        attempts = 0
        sleeps: list[float] = []

        def connect(*args: object, **kwargs: object) -> TestStartApiTunnel._Socket:
            nonlocal attempts
            attempts += 1
            if attempts < 3:
                raise ConnectionRefusedError(61, "Connection refused")
            return self._Socket()

        monkeypatch.setattr(ssm_tunnel.subprocess, "Popen", lambda *a, **k: _Proc())
        monkeypatch.setattr(ssm_tunnel.socket, "create_connection", connect)
        monkeypatch.setattr(ssm_tunnel.time, "sleep", sleeps.append)

        ssm_tunnel.start_api_tunnel(
            "i-0123456789abcdef0",
            "https://ABC.gr7.us-east-1.eks.amazonaws.com",
            8443,
            "us-east-1",
            ready_wait_seconds=5,
            ready_poll_seconds=0.1,
        )

        assert attempts == 3
        assert sleeps == [0.1, 0.1]

    def test_reports_session_exit_while_waiting(self, monkeypatch: pytest.MonkeyPatch) -> None:
        class _Proc:
            def __init__(self) -> None:
                self.polls = 0

            def poll(self) -> int | None:
                self.polls += 1
                return None if self.polls == 1 else 23

            def communicate(self, timeout: float | None = None) -> tuple[bytes, bytes]:
                return b"", b"Session Manager channel failed"

        def refused(*args: object, **kwargs: object) -> None:
            raise ConnectionRefusedError(61, "Connection refused")

        monkeypatch.setattr(ssm_tunnel.subprocess, "Popen", lambda *a, **k: _Proc())
        monkeypatch.setattr(ssm_tunnel.socket, "create_connection", refused)
        monkeypatch.setattr(ssm_tunnel.time, "sleep", lambda _seconds: None)

        with pytest.raises(RuntimeError, match="exit code 23.*Session Manager channel failed"):
            ssm_tunnel.start_api_tunnel(
                "i-0123456789abcdef0",
                "https://ABC.gr7.us-east-1.eks.amazonaws.com",
                8443,
                "us-east-1",
                ready_wait_seconds=5,
            )

    def test_timeout_terminates_and_reaps_process(self, monkeypatch: pytest.MonkeyPatch) -> None:
        class _Proc:
            def __init__(self) -> None:
                self.terminated = False
                self.reaped = False

            def poll(self) -> int | None:
                return -15 if self.terminated else None

            def terminate(self) -> None:
                self.terminated = True

            def communicate(self, timeout: float | None = None) -> tuple[bytes, bytes]:
                self.reaped = True
                return b"", b"listener setup failed"

        proc = _Proc()
        clock = iter((0.0, 2.0))

        def refused(*args: object, **kwargs: object) -> None:
            raise ConnectionRefusedError(61, "Connection refused")

        monkeypatch.setattr(ssm_tunnel.subprocess, "Popen", lambda *a, **k: proc)
        monkeypatch.setattr(ssm_tunnel.socket, "create_connection", refused)
        monkeypatch.setattr(ssm_tunnel.time, "monotonic", lambda: next(clock))

        with pytest.raises(RuntimeError, match="did not accept local connections") as exc_info:
            ssm_tunnel.start_api_tunnel(
                "i-0123456789abcdef0",
                "https://ABC.gr7.us-east-1.eks.amazonaws.com",
                8443,
                "us-east-1",
                ready_wait_seconds=1,
            )
        assert "listener setup failed" in str(exc_info.value)
        assert proc.terminated is True
        assert proc.reaped is True

    def test_stop_escalates_complete_posix_process_group(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        class _Proc:
            pid = 4321
            stdout = None
            stderr = None

            def __init__(self) -> None:
                self.communications = 0

            def poll(self) -> None:
                return None

            def communicate(self, timeout: float | None = None) -> tuple[bytes, bytes]:
                self.communications += 1
                if self.communications == 1:
                    raise ssm_tunnel.subprocess.TimeoutExpired("aws", timeout)
                return b"stdout", b"stderr"

        signals: list[tuple[int, int]] = []
        monkeypatch.setattr(ssm_tunnel.os, "name", "posix")
        monkeypatch.setattr(
            ssm_tunnel.os,
            "killpg",
            lambda pid, sent_signal: signals.append((pid, sent_signal)),
        )

        stdout, stderr = ssm_tunnel.stop_api_tunnel(_Proc(), wait_seconds=1)

        assert signals == [
            (4321, ssm_tunnel.signal.SIGTERM),
            (4321, ssm_tunnel.signal.SIGKILL),
        ]
        assert (stdout, stderr) == (b"stdout", b"stderr")

    def test_stop_forces_windows_tree_before_wrapper_fallback(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        class _Proc:
            pid = 4321
            stdout = None
            stderr = None

            def __init__(self) -> None:
                self.communications = 0
                self.terminate_calls = 0
                self.kill_calls = 0

            def poll(self) -> None:
                return None

            def terminate(self) -> None:
                self.terminate_calls += 1

            def kill(self) -> None:
                self.kill_calls += 1

            def communicate(self, timeout: float | None = None) -> tuple[bytes, bytes]:
                self.communications += 1
                if self.communications == 1:
                    raise ssm_tunnel.subprocess.TimeoutExpired("aws", timeout)
                return b"", b""

        class _Result:
            def __init__(self, returncode: int) -> None:
                self.returncode = returncode

        commands: list[list[str]] = []

        def taskkill(command: list[str], **kwargs: object) -> _Result:
            commands.append(command)
            return _Result(1 if len(commands) == 1 else 0)

        proc = _Proc()
        monkeypatch.setattr(ssm_tunnel.os, "name", "nt")
        monkeypatch.setattr(ssm_tunnel.shutil, "which", lambda _name: "taskkill.exe")
        monkeypatch.setattr(ssm_tunnel.subprocess, "run", taskkill)

        ssm_tunnel.stop_api_tunnel(proc, wait_seconds=1)

        assert commands == [
            ["taskkill.exe", "/PID", "4321", "/T"],
            ["taskkill.exe", "/PID", "4321", "/T", "/F"],
        ]
        assert proc.terminate_calls == 0
        assert proc.kill_calls == 0

    def test_interrupted_startup_terminates_and_reaps_process(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        class _Proc:
            def __init__(self) -> None:
                self.terminated = False
                self.reaped = False

            def poll(self) -> int | None:
                return -15 if self.terminated else None

            def terminate(self) -> None:
                self.terminated = True

            def communicate(self, timeout: float | None = None) -> tuple[bytes, bytes]:
                self.reaped = True
                return b"", b""

        proc = _Proc()

        def interrupt(*args: object, **kwargs: object) -> None:
            raise KeyboardInterrupt

        monkeypatch.setattr(ssm_tunnel.subprocess, "Popen", lambda *a, **k: proc)
        monkeypatch.setattr(ssm_tunnel.socket, "create_connection", interrupt)

        with pytest.raises(KeyboardInterrupt):
            ssm_tunnel.start_api_tunnel(
                "i-0123456789abcdef0",
                "https://ABC.gr7.us-east-1.eks.amazonaws.com",
                8443,
                "us-east-1",
            )
        assert proc.terminated is True
        assert proc.reaped is True

    def test_raises_when_session_dies_immediately(self, monkeypatch: pytest.MonkeyPatch) -> None:
        class _Proc:
            def poll(self) -> int:
                return 1

            def communicate(self, timeout: float | None = None) -> tuple[bytes, bytes]:
                return b"", b"SessionManagerPlugin not found"

        monkeypatch.setattr(ssm_tunnel.subprocess, "Popen", lambda *a, **k: _Proc())
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

        monkeypatch.setattr(ssm_tunnel.subprocess, "Popen", _boom)
        with pytest.raises(RuntimeError, match="AWS CLI not found"):
            ssm_tunnel.start_api_tunnel(
                "i-0123456789abcdef0", "h.example.com", 8443, "us-east-1", ready_wait_seconds=0
            )


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
            "cli.commands.monitoring_cmd._exec_port_forward",
            lambda cmd: captured.__setitem__("cmd", cmd),
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

        monkeypatch.setattr("cli.ssm_tunnel.start_api_tunnel", _boom)
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


def test_exec_port_forward_invokes_subprocess(monkeypatch: pytest.MonkeyPatch) -> None:
    import cli.commands.monitoring_cmd as m

    called: dict[str, object] = {}
    monkeypatch.setattr(m.subprocess, "run", lambda *a, **k: called.setdefault("cmd", a[0]))
    m._exec_port_forward(["kubectl", "port-forward"])
    assert called["cmd"] == ["kubectl", "port-forward"]


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
        "cli.commands.monitoring_cmd._exec_port_forward",
        lambda cmd: captured.__setitem__("cmd", cmd),
    )
    monkeypatch.setattr("cli.config._load_cdk_json", lambda: {"regional": ["us-west-2"]})
    result = runner.invoke(cli, ["monitoring", "open", "--service", "prometheus"])
    assert result.exit_code == 0, result.output
    assert captured["region"] == "us-west-2"
    assert captured["cluster"].endswith("-us-west-2")
    assert "svc/kube-prometheus-stack-prometheus" in captured["cmd"]
