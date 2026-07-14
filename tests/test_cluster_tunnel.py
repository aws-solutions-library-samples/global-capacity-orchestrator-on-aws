"""Tests for the shared cluster-tunnel core (cli/cluster_tunnel.py), the
`gco cluster tunnel` command (cli/commands/cluster_cmd.py), and the
`--via-ssm auto` path of `gco monitoring open`.

The pure ``TunnelPlan`` builders (ssm_command / kubectl_flags / as_dict) are
tested directly — they back both ``--print`` and the MCP connection-plan tool.
The ``open_api_server_tunnel`` context manager is tested across every branch
(public / private+id / private+auto / private+none / lookup failure / tunnel
failure after auto-provision), with the AWS + bastion seams mocked so the
lifecycle (and its guaranteed teardown) is verified without touching AWS.
"""

from __future__ import annotations

import json

import pytest
from click.testing import CliRunner

from cli import cluster_tunnel as ct
from cli.main import cli

PRIVATE_ENDPOINT = "https://ABC123.gr7.us-east-1.eks.amazonaws.com"
PRIVATE_HOST = "ABC123.gr7.us-east-1.eks.amazonaws.com"


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


class _FakeTunnel:
    """Stand-in for the SSM tunnel Popen; records termination."""

    def __init__(self) -> None:
        self.terminated = False

    def terminate(self) -> None:
        self.terminated = True


class _FakeFormatter:
    """Minimal formatter capturing messages for assertions."""

    def __init__(self) -> None:
        self.messages: list[tuple[str, str]] = []

    def print_info(self, m: str) -> None:
        self.messages.append(("info", m))

    def print_success(self, m: str) -> None:
        self.messages.append(("success", m))

    def print_warning(self, m: str) -> None:
        self.messages.append(("warning", m))

    def print_error(self, m: str) -> None:
        self.messages.append(("error", m))

    def text(self) -> str:
        return "\n".join(m for _, m in self.messages)


def _private_plan() -> ct.TunnelPlan:
    return ct.TunnelPlan(
        cluster="gco-us-east-1",
        region="us-east-1",
        endpoint=PRIVATE_ENDPOINT,
        public=False,
        private=True,
    )


# ---------------------------------------------------------------------------
# TunnelPlan (pure)
# ---------------------------------------------------------------------------


class TestTunnelPlan:
    def test_endpoint_host_parses(self) -> None:
        assert _private_plan().endpoint_host == PRIVATE_HOST

    def test_endpoint_host_empty_when_no_endpoint(self) -> None:
        plan = ct.TunnelPlan("c", "us-east-1", "", public=True, private=False)
        assert plan.endpoint_host == ""

    def test_ssm_command_argv(self) -> None:
        cmd = _private_plan().ssm_command("i-0123456789abcdef0")
        assert cmd[:5] == ["aws", "ssm", "start-session", "--target", "i-0123456789abcdef0"]
        params = json.loads(cmd[cmd.index("--parameters") + 1])
        assert params["host"] == [PRIVATE_HOST]
        assert params["localPortNumber"] == ["8443"]

    def test_kubectl_flags(self) -> None:
        flags = _private_plan().kubectl_flags()
        assert flags == [
            "--server",
            "https://localhost:8443",
            "--tls-server-name",
            PRIVATE_HOST,
        ]

    def test_as_dict_public_is_direct(self) -> None:
        plan = ct.TunnelPlan(
            "gco-us-east-1", "us-east-1", "https://x.eks.amazonaws.com", True, False
        )
        d = plan.as_dict()
        assert d["reachable"] == "direct"
        assert "ssm_command" not in d and "ssm_command_template" not in d
        assert "kubectl" in d["note"].lower()

    def test_as_dict_private_with_instance(self) -> None:
        d = _private_plan().as_dict("i-0123456789abcdef0")
        assert d["reachable"] == "ssm-tunnel"
        assert d["ssm_command_str"].startswith("aws ssm start-session")
        assert "i-0123456789abcdef0" in d["ssm_command_str"]
        assert d["kubectl_flags"][1] == "https://localhost:8443"

    def test_as_dict_private_without_instance_uses_template(self) -> None:
        d = _private_plan().as_dict()
        assert "ssm_command" not in d
        assert "<INSTANCE_ID>" in d["ssm_command_template"]
        assert "auto" in d["note"]


# ---------------------------------------------------------------------------
# resolve_tunnel_plan / resolve_region
# ---------------------------------------------------------------------------


class TestResolvers:
    def test_resolve_tunnel_plan(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            ct.kubectl_helpers,
            "describe_cluster_access",
            lambda c, r: {"endpoint": PRIVATE_ENDPOINT, "public": False, "private": True},
        )
        plan = ct.resolve_tunnel_plan("gco-us-east-1", "us-east-1")
        assert plan.private is True and plan.endpoint == PRIVATE_ENDPOINT

    def test_resolve_region_explicit(self) -> None:
        class _Cfg:
            default_region = "us-west-2"

        assert ct.resolve_region(_Cfg(), "eu-west-1") == "eu-west-1"

    def test_resolve_region_from_cdk_json(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("cli.config._load_cdk_json", lambda: {"regional": ["ap-south-1"]})

        class _Cfg:
            default_region = None

        assert ct.resolve_region(_Cfg(), None) == "ap-south-1"

    def test_resolve_region_default(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("cli.config._load_cdk_json", lambda: {})

        class _Cfg:
            default_region = "us-east-2"

        assert ct.resolve_region(_Cfg(), None) == "us-east-2"


# ---------------------------------------------------------------------------
# provision_bastion / teardown_bastion
# ---------------------------------------------------------------------------


class TestBastionHelpers:
    def test_provision_with_yes_skips_confirm(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            ct.ephemeral_bastion,
            "create_ephemeral_bastion",
            lambda c, r, **k: "i-0123456789abcdef0",
        )
        fmt = _FakeFormatter()
        out = ct.provision_bastion(fmt, "gco-us-east-1", "us-east-1", 120, assume_yes=True)
        assert out == "i-0123456789abcdef0"
        assert any("online" in m for _, m in fmt.messages)

    def test_provision_confirm_abort(self, monkeypatch: pytest.MonkeyPatch) -> None:
        created: list[str] = []
        monkeypatch.setattr(
            ct.ephemeral_bastion,
            "create_ephemeral_bastion",
            lambda c, r, **k: created.append("x") or "i-0123456789abcdef0",
        )

        import click

        def _abort(*a: object, **k: object) -> None:
            raise click.exceptions.Abort()

        monkeypatch.setattr(ct.click, "confirm", _abort)
        fmt = _FakeFormatter()
        with pytest.raises(click.exceptions.Abort):
            ct.provision_bastion(fmt, "gco-us-east-1", "us-east-1", 120, assume_yes=False)
        assert created == []  # never launched

    def test_teardown_success(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            ct.ephemeral_bastion, "destroy_ephemeral_bastion", lambda iid, r, **k: None
        )
        fmt = _FakeFormatter()
        ct.teardown_bastion(fmt, "i-0123456789abcdef0", "us-east-1")
        assert any("terminated" in m for _, m in fmt.messages)

    def test_teardown_failure_prints_orphan_check(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def _boom(*a: object, **k: object) -> None:
            raise RuntimeError("api error")

        monkeypatch.setattr(ct.ephemeral_bastion, "destroy_ephemeral_bastion", _boom)
        fmt = _FakeFormatter()
        ct.teardown_bastion(fmt, "i-0123456789abcdef0", "us-east-1")
        errors = [m for lvl, m in fmt.messages if lvl == "error"]
        assert errors and "gco:ephemeral" in errors[0]


# ---------------------------------------------------------------------------
# open_api_server_tunnel context manager
# ---------------------------------------------------------------------------


class TestOpenApiServerTunnel:
    def test_public_endpoint_no_tunnel(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            ct.kubectl_helpers,
            "describe_cluster_access",
            lambda c, r: {
                "endpoint": "https://x.eks.amazonaws.com",
                "public": True,
                "private": False,
            },
        )
        fmt = _FakeFormatter()
        with ct.open_api_server_tunnel(
            fmt, cluster="gco-us-east-1", region="us-east-1", via_ssm=None
        ) as session:
            assert session.server is None
            assert session.active is False

    def test_private_with_instance_tunnels(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            ct.kubectl_helpers,
            "describe_cluster_access",
            lambda c, r: {"endpoint": PRIVATE_ENDPOINT, "public": False, "private": True},
        )
        proc = _FakeTunnel()
        started: dict[str, object] = {}

        def _fake_start(instance, endpoint, local_port, region, **k):
            started["instance"] = instance
            return proc

        monkeypatch.setattr(ct.ssm_tunnel, "start_api_tunnel", _fake_start)
        fmt = _FakeFormatter()
        with ct.open_api_server_tunnel(
            fmt, cluster="gco-us-east-1", region="us-east-1", via_ssm="i-0123456789abcdef0"
        ) as session:
            assert session.server == "https://localhost:8443"
            assert session.tls_server_name == PRIVATE_HOST
            assert session.active is True
        assert started["instance"] == "i-0123456789abcdef0"
        assert proc.terminated is True  # torn down on exit

    def test_private_auto_provisions_and_tears_down(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            ct.kubectl_helpers,
            "describe_cluster_access",
            lambda c, r: {"endpoint": PRIVATE_ENDPOINT, "public": False, "private": True},
        )
        proc = _FakeTunnel()
        torn: list[str] = []
        monkeypatch.setattr(ct, "provision_bastion", lambda *a, **k: "i-0aaaaaaaaaaaaaaaa")
        monkeypatch.setattr(ct, "teardown_bastion", lambda fmt, iid, r: torn.append(iid))
        monkeypatch.setattr(ct.ssm_tunnel, "start_api_tunnel", lambda *a, **k: proc)
        fmt = _FakeFormatter()
        with ct.open_api_server_tunnel(
            fmt, cluster="gco-us-east-1", region="us-east-1", via_ssm="auto", assume_yes=True
        ) as session:
            assert session.active is True
        assert torn == ["i-0aaaaaaaaaaaaaaaa"]
        assert proc.terminated is True

    def test_private_without_instance_warns(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            ct.kubectl_helpers,
            "describe_cluster_access",
            lambda c, r: {"endpoint": PRIVATE_ENDPOINT, "public": False, "private": True},
        )
        fmt = _FakeFormatter()
        with ct.open_api_server_tunnel(
            fmt, cluster="gco-us-east-1", region="us-east-1", via_ssm=None
        ) as session:
            assert session.server is None
            assert session.active is False
        assert any("PRIVATE API endpoint" in m for _, m in fmt.messages)

    def test_lookup_failure_falls_back_to_direct(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def _boom(c: str, r: str) -> dict[str, object]:
            raise RuntimeError("describe failed")

        monkeypatch.setattr(ct.kubectl_helpers, "describe_cluster_access", _boom)
        fmt = _FakeFormatter()
        with ct.open_api_server_tunnel(
            fmt, cluster="gco-us-east-1", region="us-east-1", via_ssm=None
        ) as session:
            assert session.server is None
        assert any("Could not determine endpoint access mode" in m for _, m in fmt.messages)

    def test_tunnel_failure_after_auto_tears_down_bastion(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            ct.kubectl_helpers,
            "describe_cluster_access",
            lambda c, r: {"endpoint": PRIVATE_ENDPOINT, "public": False, "private": True},
        )
        torn: list[str] = []
        monkeypatch.setattr(ct, "provision_bastion", lambda *a, **k: "i-0aaaaaaaaaaaaaaaa")
        monkeypatch.setattr(ct, "teardown_bastion", lambda fmt, iid, r: torn.append(iid))

        def _boom(*a: object, **k: object) -> None:
            raise RuntimeError("tunnel failed")

        monkeypatch.setattr(ct.ssm_tunnel, "start_api_tunnel", _boom)
        fmt = _FakeFormatter()
        with (
            pytest.raises(RuntimeError, match="tunnel failed"),
            ct.open_api_server_tunnel(
                fmt, cluster="gco-us-east-1", region="us-east-1", via_ssm="auto", assume_yes=True
            ),
        ):
            pass
        # The just-provisioned bastion is not leaked when the tunnel fails.
        assert torn == ["i-0aaaaaaaaaaaaaaaa"]


# ---------------------------------------------------------------------------
# gco cluster tunnel --print
# ---------------------------------------------------------------------------


class TestClusterTunnelPrint:
    def _patch_access(self, monkeypatch: pytest.MonkeyPatch, access: dict) -> None:
        monkeypatch.setattr("cli.kubectl_helpers.describe_cluster_access", lambda c, r: access)

    def test_print_json_private_no_instance(
        self, runner: CliRunner, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._patch_access(
            monkeypatch, {"endpoint": PRIVATE_ENDPOINT, "public": False, "private": True}
        )
        res = runner.invoke(
            cli, ["--output", "json", "cluster", "tunnel", "--print", "--region", "us-east-1"]
        )
        assert res.exit_code == 0, res.output
        payload = json.loads(res.output)
        assert payload["private"] is True
        assert "<INSTANCE_ID>" in payload["ssm_command_template"]

    def test_print_json_with_instance(
        self, runner: CliRunner, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._patch_access(
            monkeypatch, {"endpoint": PRIVATE_ENDPOINT, "public": False, "private": True}
        )
        res = runner.invoke(
            cli,
            [
                "--output",
                "json",
                "cluster",
                "tunnel",
                "--print",
                "--region",
                "us-east-1",
                "--via-ssm",
                "i-0123456789abcdef0",
            ],
        )
        assert res.exit_code == 0, res.output
        payload = json.loads(res.output)
        assert "i-0123456789abcdef0" in payload["ssm_command_str"]

    def test_print_human_private(self, runner: CliRunner, monkeypatch: pytest.MonkeyPatch) -> None:
        self._patch_access(
            monkeypatch, {"endpoint": PRIVATE_ENDPOINT, "public": False, "private": True}
        )
        res = runner.invoke(cli, ["cluster", "tunnel", "--print", "--region", "us-east-1"])
        assert res.exit_code == 0, res.output
        assert "PRIVATE endpoint" in res.output
        assert "aws ssm start-session" in res.output

    def test_print_public(self, runner: CliRunner, monkeypatch: pytest.MonkeyPatch) -> None:
        self._patch_access(
            monkeypatch,
            {"endpoint": "https://x.eks.amazonaws.com", "public": True, "private": False},
        )
        res = runner.invoke(
            cli, ["--output", "json", "cluster", "tunnel", "--print", "--region", "us-east-1"]
        )
        assert res.exit_code == 0, res.output
        assert json.loads(res.output)["reachable"] == "direct"

    def test_print_human_public(self, runner: CliRunner, monkeypatch: pytest.MonkeyPatch) -> None:
        self._patch_access(
            monkeypatch,
            {"endpoint": "https://x.eks.amazonaws.com", "public": True, "private": False},
        )
        res = runner.invoke(cli, ["cluster", "tunnel", "--print", "--region", "us-east-1"])
        assert res.exit_code == 0, res.output
        assert "PUBLIC endpoint" in res.output
        assert "update-kubeconfig" in res.output

    def test_print_resolve_failure_exits(
        self, runner: CliRunner, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def _boom(c: str, r: str) -> dict[str, object]:
            raise RuntimeError("no such cluster")

        monkeypatch.setattr("cli.kubectl_helpers.describe_cluster_access", _boom)
        res = runner.invoke(cli, ["cluster", "tunnel", "--print", "--region", "us-east-1"])
        assert res.exit_code == 1
        assert "Failed to resolve tunnel plan" in res.output


# ---------------------------------------------------------------------------
# gco cluster tunnel (interactive)
# ---------------------------------------------------------------------------


class TestClusterTunnelInteractive:
    def test_private_with_instance_holds_open(
        self, runner: CliRunner, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr("cli.kubectl_helpers.update_kubeconfig", lambda c, r: None)
        monkeypatch.setattr(
            "cli.kubectl_helpers.describe_cluster_access",
            lambda c, r: {"endpoint": PRIVATE_ENDPOINT, "public": False, "private": True},
        )
        proc = _FakeTunnel()
        monkeypatch.setattr("cli.ssm_tunnel.start_api_tunnel", lambda *a, **k: proc)
        blocked: dict[str, bool] = {}
        monkeypatch.setattr(
            "cli.commands.cluster_cmd._block_until_interrupt",
            lambda: blocked.setdefault("waited", True),
        )
        res = runner.invoke(
            cli, ["cluster", "tunnel", "--via-ssm", "i-0123456789abcdef0", "--region", "us-east-1"]
        )
        assert res.exit_code == 0, res.output
        assert "SSM tunnel open" in res.output
        assert "kubectl --server https://localhost:8443" in res.output
        assert blocked.get("waited") is True
        assert proc.terminated is True

    def test_public_endpoint_no_tunnel_needed(
        self, runner: CliRunner, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr("cli.kubectl_helpers.update_kubeconfig", lambda c, r: None)
        monkeypatch.setattr(
            "cli.kubectl_helpers.describe_cluster_access",
            lambda c, r: {
                "endpoint": "https://x.eks.amazonaws.com",
                "public": True,
                "private": False,
            },
        )

        def _fail() -> None:
            raise AssertionError("must not block on a public endpoint")

        monkeypatch.setattr("cli.commands.cluster_cmd._block_until_interrupt", _fail)
        res = runner.invoke(cli, ["cluster", "tunnel", "--region", "us-east-1"])
        assert res.exit_code == 0, res.output
        assert "PUBLIC API endpoint" in res.output

    def test_private_without_instance_does_not_block(
        self, runner: CliRunner, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr("cli.kubectl_helpers.update_kubeconfig", lambda c, r: None)
        monkeypatch.setattr(
            "cli.kubectl_helpers.describe_cluster_access",
            lambda c, r: {"endpoint": PRIVATE_ENDPOINT, "public": False, "private": True},
        )

        def _fail() -> None:
            raise AssertionError("must not block without a tunnel")

        monkeypatch.setattr("cli.commands.cluster_cmd._block_until_interrupt", _fail)
        # No --via-ssm: the context manager prints guidance and yields no tunnel.
        res = runner.invoke(cli, ["cluster", "tunnel", "--region", "us-east-1"])
        assert res.exit_code == 0, res.output
        assert "PRIVATE API endpoint" in res.output

    def test_update_kubeconfig_failure_exits(
        self, runner: CliRunner, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def _boom(c: str, r: str) -> None:
            raise RuntimeError("kubeconfig failed")

        monkeypatch.setattr("cli.kubectl_helpers.update_kubeconfig", _boom)
        res = runner.invoke(cli, ["cluster", "tunnel", "--region", "us-east-1"])
        assert res.exit_code == 1
        assert "kubeconfig failed" in res.output

    def test_tunnel_failure_exits(
        self, runner: CliRunner, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr("cli.kubectl_helpers.update_kubeconfig", lambda c, r: None)
        monkeypatch.setattr(
            "cli.kubectl_helpers.describe_cluster_access",
            lambda c, r: {"endpoint": PRIVATE_ENDPOINT, "public": False, "private": True},
        )

        def _boom(*a: object, **k: object) -> None:
            raise RuntimeError("tunnel start failed")

        monkeypatch.setattr("cli.ssm_tunnel.start_api_tunnel", _boom)
        res = runner.invoke(
            cli, ["cluster", "tunnel", "--via-ssm", "i-0123456789abcdef0", "--region", "us-east-1"]
        )
        assert res.exit_code == 1
        assert "tunnel start failed" in res.output


# ---------------------------------------------------------------------------
# gco monitoring open --via-ssm auto  (the bastion auto path end-to-end)
# ---------------------------------------------------------------------------


class TestMonitoringOpenAutoBastion:
    def test_auto_provisions_tunnels_and_tears_down(
        self, runner: CliRunner, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr("cli.kubectl_helpers.update_kubeconfig", lambda c, r: None)
        monkeypatch.setattr(
            "cli.kubectl_helpers.describe_cluster_access",
            lambda c, r: {"endpoint": PRIVATE_ENDPOINT, "public": False, "private": True},
        )
        created: list[str] = []
        destroyed: list[str] = []
        monkeypatch.setattr(
            "cli.ephemeral_bastion.create_ephemeral_bastion",
            lambda c, r, **k: created.append("x") or "i-0aaaaaaaaaaaaaaaa",
        )
        monkeypatch.setattr(
            "cli.ephemeral_bastion.destroy_ephemeral_bastion",
            lambda iid, r, **k: destroyed.append(iid),
        )
        monkeypatch.setattr("cli.ssm_tunnel.start_api_tunnel", lambda *a, **k: _FakeTunnel())
        captured: dict[str, object] = {}
        monkeypatch.setattr(
            "cli.commands.monitoring_cmd._exec_port_forward",
            lambda cmd: captured.__setitem__("cmd", cmd),
        )
        res = runner.invoke(
            cli,
            ["monitoring", "open", "--via-ssm", "auto", "-y", "--region", "us-east-1"],
        )
        assert res.exit_code == 0, res.output
        assert created == ["x"]  # bastion provisioned
        assert destroyed == ["i-0aaaaaaaaaaaaaaaa"]  # and torn down
        # Port-forward routed through the SSM tunnel (server override present).
        assert "--server" in captured["cmd"]
        assert "https://localhost:8443" in captured["cmd"]
