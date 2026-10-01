"""Tests for the shared cluster-tunnel core (cli/cluster_tunnel.py), the
`gco cluster tunnel` command (cli/commands/cluster_cmd.py), and the
`--via-ssm auto` path of `gco monitoring open`.

The pure ``TunnelPlan`` builders (ssm_command / kubectl_flags / as_dict) are
tested directly — they back both ``--print`` and the MCP connection-plan tool.
The ``open_api_server_tunnel`` context manager is tested across every branch
(public / private+id / private+auto / private+none / lookup failure / tunnel
failure after auto-provision), with the AWS + bastion seams mocked so the
lifecycle (and its guaranteed teardown) is verified without touching AWS.

``TestRouteMatrix`` pins the route for every endpoint access mode crossed with
every ``--via-ssm`` value: an explicit instance id or ``auto`` tunnels to the
private endpoint whenever private access is on (``PUBLIC_AND_PRIVATE``
included), no ``--via-ssm`` uses a public endpoint directly, and an explicit
request a cluster cannot honor fails before anything is provisioned.
"""

from __future__ import annotations

import json
from typing import Any

import click
import pytest
from click.testing import CliRunner

from cli import cluster_tunnel as ct
from cli.main import cli

PRIVATE_ENDPOINT = "https://ABC123.gr7.us-east-1.eks.amazonaws.com"
PRIVATE_HOST = "ABC123.gr7.us-east-1.eks.amazonaws.com"
INSTANCE = "i-0123456789abcdef0"
BASTION = "i-0aaaaaaaaaaaaaaaa"

# describe_cluster_access results for each access mode EKS can report.
ACCESS: dict[str, dict[str, object]] = {
    "PRIVATE": {"endpoint": PRIVATE_ENDPOINT, "public": False, "private": True},
    "PUBLIC_AND_PRIVATE": {"endpoint": PRIVATE_ENDPOINT, "public": True, "private": True},
    "PUBLIC": {"endpoint": PRIVATE_ENDPOINT, "public": True, "private": False},
}


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


class _FakeTunnel:
    """Stand-in for the SSM tunnel Popen; records termination and reaping."""

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
            "https://127.0.0.1:8443",
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
        assert d["kubectl_flags"][1] == "https://127.0.0.1:8443"

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
        monkeypatch.setattr("cli.config._load_cdk_json", dict)

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

        monkeypatch.setattr(ct, "confirm", _abort)
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
            assert session.server == "https://127.0.0.1:8443"
            assert session.tls_server_name == PRIVATE_HOST
            assert session.active is True
        assert started["instance"] == "i-0123456789abcdef0"
        assert proc.terminated is True  # torn down on exit
        assert proc.reaped is True

    def test_private_auto_provisions_and_tears_down(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            ct.kubectl_helpers,
            "describe_cluster_access",
            lambda c, r: {"endpoint": PRIVATE_ENDPOINT, "public": False, "private": True},
        )
        proc = _FakeTunnel()
        torn: list[str] = []
        monkeypatch.setattr(ct, "provision_bastion", lambda *a, **k: "i-0aaaaaaaaaaaaaaaa")
        monkeypatch.setattr(ct, "teardown_bastion", lambda fmt, iid, r, project: torn.append(iid))
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
        monkeypatch.setattr(ct, "teardown_bastion", lambda fmt, iid, r, project: torn.append(iid))

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
        assert "kubectl --server https://127.0.0.1:8443" in res.output
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

    def test_tunnel_failure_exits(self, runner: CliRunner, monkeypatch: pytest.MonkeyPatch) -> None:
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
        captured: dict[str, Any] = {}
        monkeypatch.setattr("cli.cluster_ui.exec_port_forward", _ready_forward(captured))
        res = runner.invoke(
            cli,
            ["monitoring", "open", "--via-ssm", "auto", "-y", "--region", "us-east-1"],
        )
        assert res.exit_code == 0, res.output
        assert created == ["x"]  # bastion provisioned
        assert destroyed == ["i-0aaaaaaaaaaaaaaaa"]  # and torn down
        # Port-forward routed through the SSM tunnel (server override present).
        assert "--server" in captured["cmd"]
        assert "https://127.0.0.1:8443" in captured["cmd"]


def _ready_forward(captured: dict[str, Any], *, fail: str | None = None) -> Any:
    """A stand-in for cluster_ui.exec_port_forward.

    It records the argv and port, then either reports kubectl's listener
    (calls ``on_ready``) or, with ``fail``, raises the way a kubectl that never
    listened does — without ever calling ``on_ready``.
    """

    def forward(cmd: list[str], local_port: int, *, on_ready: Any = None, **_: Any) -> None:
        captured["cmd"] = cmd
        captured["port"] = local_port
        if fail is not None:
            raise RuntimeError(fail)
        if on_ready is not None:
            on_ready()

    return forward


# ---------------------------------------------------------------------------
# Project-name derivation (bastion IAM naming is project-scoped)
# ---------------------------------------------------------------------------


class TestProjectNameDerivation:
    def test_project_name_from_cluster(self) -> None:
        assert ct._project_name_from_cluster("gco-us-east-1", "us-east-1") == "gco"
        assert ct._project_name_from_cluster("acme-corp-eu-west-1", "eu-west-1") == "acme-corp"
        # No matching region suffix → returned unchanged.
        assert ct._project_name_from_cluster("weird", "us-east-1") == "weird"

    def test_open_tunnel_passes_derived_project(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            ct.kubectl_helpers,
            "describe_cluster_access",
            lambda c, r: {"endpoint": PRIVATE_ENDPOINT, "public": False, "private": True},
        )
        seen: dict[str, object] = {}
        monkeypatch.setattr(
            ct,
            "provision_bastion",
            lambda fmt, c, r, ttl, yes, project: (
                seen.__setitem__("prov", project) or "i-0aaaaaaaaaaaaaaaa"
            ),
        )
        monkeypatch.setattr(
            ct, "teardown_bastion", lambda fmt, iid, r, project: seen.__setitem__("tear", project)
        )
        monkeypatch.setattr(ct.ssm_tunnel, "start_api_tunnel", lambda *a, **k: _FakeTunnel())
        fmt = _FakeFormatter()
        with ct.open_api_server_tunnel(
            fmt, cluster="acme-us-east-1", region="us-east-1", via_ssm="auto", assume_yes=True
        ):
            pass
        # The project key recovered from the cluster name flows to both the
        # provision and teardown paths (so IAM naming stays project-scoped).
        assert seen["prov"] == "acme"
        assert seen["tear"] == "acme"


# ---------------------------------------------------------------------------
# Access modes and the connection plan for PUBLIC_AND_PRIVATE
# ---------------------------------------------------------------------------


def _plan(mode: str, endpoint: str = PRIVATE_ENDPOINT) -> ct.TunnelPlan:
    access = ACCESS[mode]
    return ct.TunnelPlan(
        cluster="gco-us-east-1",
        region="us-east-1",
        endpoint=endpoint,
        public=bool(access["public"]),
        private=bool(access["private"]),
    )


class TestAccessMode:
    @pytest.mark.parametrize(
        ("public", "private", "mode", "private_access"),
        [
            (False, True, "PRIVATE", True),
            (True, True, "PUBLIC_AND_PRIVATE", True),
            (True, False, "PUBLIC", False),
            # EKS never turns both off, so no public access means private.
            (False, False, "PRIVATE", True),
        ],
    )
    def test_mode_and_private_access(
        self, public: bool, private: bool, mode: str, private_access: bool
    ) -> None:
        plan = ct.TunnelPlan("c", "us-east-1", PRIVATE_ENDPOINT, public=public, private=private)
        assert plan.access_mode == mode
        assert plan.private_access is private_access

    def test_require_private_access_rejects_a_public_only_endpoint(self) -> None:
        with pytest.raises(RuntimeError) as excinfo:
            ct.require_private_access(_plan("PUBLIC"), INSTANCE)
        message = str(excinfo.value)
        assert f"--via-ssm {INSTANCE}" in message
        assert "endpointPrivateAccess=false" in message
        assert "Re-run without --via-ssm" in message
        assert "gco stacks deploy gco-us-east-1 -y" in message

    @pytest.mark.parametrize("mode", ["PRIVATE", "PUBLIC_AND_PRIVATE"])
    def test_require_private_access_accepts_private_endpoints(self, mode: str) -> None:
        ct.require_private_access(_plan(mode), ct.AUTO_BASTION)

    def test_public_and_private_without_via_ssm_plans_the_direct_route(self) -> None:
        d = _plan("PUBLIC_AND_PRIVATE").as_dict()
        assert d["reachable"] == ct.ROUTE_DIRECT
        assert d["access_mode"] == "PUBLIC_AND_PRIVATE"
        assert "ssm_command" not in d and "ssm_command_template" not in d
        # The private route stays discoverable from the plan.
        assert "--via-ssm <instance-id>" in d["note"]
        assert "CIDR allowlist" in d["note"]

    def test_public_only_plan_offers_no_ssm_route(self) -> None:
        d = _plan("PUBLIC").as_dict()
        assert d["reachable"] == ct.ROUTE_DIRECT
        assert d["access_mode"] == "PUBLIC"
        assert "--via-ssm" not in d["note"]

    def test_public_and_private_with_an_instance_plans_the_tunnel(self) -> None:
        d = _plan("PUBLIC_AND_PRIVATE").as_dict(INSTANCE)
        assert d["reachable"] == ct.ROUTE_SSM
        assert INSTANCE in d["ssm_command_str"]
        assert d["kubectl_flags"] == [
            "--server",
            "https://127.0.0.1:8443",
            "--tls-server-name",
            PRIVATE_HOST,
        ]

    def test_public_and_private_with_auto_plans_the_tunnel_template(self) -> None:
        d = _plan("PUBLIC_AND_PRIVATE").as_dict(ct.AUTO_BASTION)
        assert d["reachable"] == ct.ROUTE_SSM
        assert "<INSTANCE_ID>" in d["ssm_command_template"]
        assert "ssm_command" not in d


# ---------------------------------------------------------------------------
# Route selection: every access mode × every --via-ssm value
# ---------------------------------------------------------------------------


class _Seams:
    """Records the describe / bastion / tunnel seams one tunnel session drives."""

    def __init__(
        self,
        monkeypatch: pytest.MonkeyPatch,
        access: dict[str, object] | None,
        *,
        tunnel_error: str | None = None,
    ) -> None:
        self.events: list[str] = []
        self.tunnel = _FakeTunnel()

        def describe(cluster: str, region: str) -> dict[str, object]:
            if access is None:
                raise RuntimeError("AccessDeniedException: eks:DescribeCluster")
            return dict(access)

        def provision(
            fmt: Any, cluster: str, region: str, ttl: int, assume_yes: bool, project: str
        ) -> str:
            self.events.append(f"provision:{ttl}:{assume_yes}")
            return BASTION

        def teardown(fmt: Any, instance_id: str, region: str, project: str) -> None:
            self.events.append(f"teardown:{instance_id}")

        def start(instance_id: str, endpoint: str, local_port: int, region: str, **_: Any) -> Any:
            self.events.append(f"start:{instance_id}:{local_port}")
            if tunnel_error is not None:
                raise RuntimeError(tunnel_error)
            return self.tunnel

        real_stop = ct.ssm_tunnel.stop_api_tunnel

        def stop(process: Any) -> tuple[bytes, bytes]:
            self.events.append("stop")
            return real_stop(process)

        monkeypatch.setattr(ct.kubectl_helpers, "describe_cluster_access", describe)
        monkeypatch.setattr(ct, "provision_bastion", provision)
        monkeypatch.setattr(ct, "teardown_bastion", teardown)
        monkeypatch.setattr(ct.ssm_tunnel, "start_api_tunnel", start)
        monkeypatch.setattr(ct.ssm_tunnel, "stop_api_tunnel", stop)


_TTL = ct.ephemeral_bastion.DEFAULT_TTL_MINUTES
_INSTANCE_EVENTS = [f"start:{INSTANCE}:8443", "body", "stop"]
_BASTION_EVENTS = [
    f"provision:{_TTL}:True",
    f"start:{BASTION}:8443",
    "body",
    "stop",
    f"teardown:{BASTION}",
]


class TestRouteMatrix:
    @pytest.mark.parametrize(
        ("mode", "via_ssm", "route", "events"),
        [
            # Private-only: unchanged — tunnel when asked, guidance otherwise.
            ("PRIVATE", None, ct.ROUTE_UNVERIFIED, ["body"]),
            ("PRIVATE", INSTANCE, ct.ROUTE_SSM, _INSTANCE_EVENTS),
            ("PRIVATE", ct.AUTO_BASTION, ct.ROUTE_SSM, _BASTION_EVENTS),
            # PUBLIC_AND_PRIVATE: direct by default, the tunnel when asked.
            ("PUBLIC_AND_PRIVATE", None, ct.ROUTE_DIRECT, ["body"]),
            ("PUBLIC_AND_PRIVATE", INSTANCE, ct.ROUTE_SSM, _INSTANCE_EVENTS),
            ("PUBLIC_AND_PRIVATE", ct.AUTO_BASTION, ct.ROUTE_SSM, _BASTION_EVENTS),
            # Public-only without --via-ssm: direct.
            ("PUBLIC", None, ct.ROUTE_DIRECT, ["body"]),
        ],
    )
    def test_route_and_lifecycle(
        self,
        monkeypatch: pytest.MonkeyPatch,
        mode: str,
        via_ssm: str | None,
        route: str,
        events: list[str],
    ) -> None:
        seams = _Seams(monkeypatch, ACCESS[mode])
        with ct.open_api_server_tunnel(
            _FakeFormatter(),
            cluster="gco-us-east-1",
            region="us-east-1",
            via_ssm=via_ssm,
            assume_yes=True,
        ) as session:
            seams.events.append("body")
            assert session.route == route
            assert session.active is (route == ct.ROUTE_SSM)
            if route == ct.ROUTE_SSM:
                assert session.server == "https://127.0.0.1:8443"
                assert session.tls_server_name == PRIVATE_HOST
                assert session.instance_id == (BASTION if via_ssm == ct.AUTO_BASTION else INSTANCE)
                assert session.process is seams.tunnel
            else:
                assert (session.server, session.tls_server_name) == (None, None)
                assert session.instance_id is None
        assert seams.events == events
        if route == ct.ROUTE_SSM:
            assert seams.tunnel.terminated and seams.tunnel.reaped

    @pytest.mark.parametrize("via_ssm", [INSTANCE, ct.AUTO_BASTION])
    def test_explicit_ssm_without_private_access_fails_before_any_setup(
        self, monkeypatch: pytest.MonkeyPatch, via_ssm: str
    ) -> None:
        seams = _Seams(monkeypatch, ACCESS["PUBLIC"])
        with (
            pytest.raises(RuntimeError, match="endpointPrivateAccess=false"),
            ct.open_api_server_tunnel(
                _FakeFormatter(),
                cluster="gco-us-east-1",
                region="us-east-1",
                via_ssm=via_ssm,
                assume_yes=True,
            ),
        ):
            pytest.fail("body must not run")  # pragma: no cover
        assert seams.events == []  # no bastion, no tunnel

    @pytest.mark.parametrize("via_ssm", [INSTANCE, ct.AUTO_BASTION])
    def test_explicit_ssm_with_an_unreadable_access_mode_fails(
        self, monkeypatch: pytest.MonkeyPatch, via_ssm: str
    ) -> None:
        seams = _Seams(monkeypatch, None)
        with (
            pytest.raises(RuntimeError, match="access mode could not be read") as excinfo,
            ct.open_api_server_tunnel(
                _FakeFormatter(), cluster="gco-us-east-1", region="us-east-1", via_ssm=via_ssm
            ),
        ):
            pytest.fail("body must not run")  # pragma: no cover
        assert "eks:DescribeCluster" in str(excinfo.value)
        assert seams.events == []

    def test_a_private_endpoint_without_a_usable_host_fails_before_provisioning(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        seams = _Seams(monkeypatch, {"endpoint": "", "public": True, "private": True})
        with (
            pytest.raises(RuntimeError, match="no usable API endpoint"),
            ct.open_api_server_tunnel(
                _FakeFormatter(),
                cluster="gco-us-east-1",
                region="us-east-1",
                via_ssm=ct.AUTO_BASTION,
                assume_yes=True,
            ),
        ):
            pytest.fail("body must not run")  # pragma: no cover
        assert seams.events == []

    def test_public_fallback_uses_a_public_only_endpoint_directly(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        seams = _Seams(monkeypatch, ACCESS["PUBLIC"])
        fmt = _FakeFormatter()
        with ct.open_api_server_tunnel(
            fmt,
            cluster="gco-us-east-1",
            region="us-east-1",
            via_ssm=ct.AUTO_BASTION,
            assume_yes=True,
            allow_public_fallback=True,
        ) as session:
            assert session.route == ct.ROUTE_DIRECT
            assert session.active is False
        assert seams.events == []
        assert "no private endpoint for --via-ssm auto to reach" in fmt.text()

    def test_public_fallback_tries_directly_when_the_access_mode_is_unreadable(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        seams = _Seams(monkeypatch, None)
        fmt = _FakeFormatter()
        with ct.open_api_server_tunnel(
            fmt,
            cluster="gco-us-east-1",
            region="us-east-1",
            via_ssm=ct.AUTO_BASTION,
            allow_public_fallback=True,
        ) as session:
            assert session.route == ct.ROUTE_UNVERIFIED
            assert session.active is False
        assert seams.events == []
        assert "Could not determine endpoint access mode" in fmt.text()

    def test_public_fallback_still_tunnels_to_a_public_and_private_endpoint(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        seams = _Seams(monkeypatch, ACCESS["PUBLIC_AND_PRIVATE"])
        with ct.open_api_server_tunnel(
            _FakeFormatter(),
            cluster="gco-us-east-1",
            region="us-east-1",
            via_ssm=ct.AUTO_BASTION,
            assume_yes=True,
            allow_public_fallback=True,
        ) as session:
            seams.events.append("body")
            assert session.route == ct.ROUTE_SSM
        assert seams.events == _BASTION_EVENTS


class TestRouteAnnouncements:
    def _messages(
        self, monkeypatch: pytest.MonkeyPatch, mode: str, via_ssm: str | None
    ) -> _FakeFormatter:
        _Seams(monkeypatch, ACCESS[mode])
        fmt = _FakeFormatter()
        with ct.open_api_server_tunnel(
            fmt, cluster="gco-us-east-1", region="us-east-1", via_ssm=via_ssm, assume_yes=True
        ):
            pass
        return fmt

    def test_public_and_private_direct_route_names_the_ssm_alternative(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        fmt = self._messages(monkeypatch, "PUBLIC_AND_PRIVATE", None)
        info = [m for level, m in fmt.messages if level == "info"]
        assert len(info) == 1
        assert info[0].startswith("Route: gco-us-east-1's public API endpoint, directly.")
        assert "--via-ssm <instance-id> or --via-ssm auto" in info[0]

    def test_public_only_direct_route_offers_no_ssm(self, monkeypatch: pytest.MonkeyPatch) -> None:
        fmt = self._messages(monkeypatch, "PUBLIC", None)
        assert fmt.messages == [("info", "Route: gco-us-east-1's public API endpoint, directly.")]

    def test_ssm_on_public_and_private_says_it_takes_precedence(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        fmt = self._messages(monkeypatch, "PUBLIC_AND_PRIVATE", INSTANCE)
        assert fmt.messages == [
            (
                "info",
                f"Route: SSM tunnel via {INSTANCE} to gco-us-east-1's private API endpoint "
                "(--via-ssm takes precedence over its public endpoint); opening it on "
                "127.0.0.1:8443...",
            )
        ]

    def test_ssm_on_a_private_endpoint_names_the_instance(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        fmt = self._messages(monkeypatch, "PRIVATE", INSTANCE)
        assert fmt.messages == [
            (
                "info",
                f"Route: SSM tunnel via {INSTANCE} to gco-us-east-1's private API endpoint; "
                "opening it on 127.0.0.1:8443...",
            )
        ]


class TestPublicAndPrivateBastionGuarantees:
    """--via-ssm auto keeps its confirmation, TTL and cleanup on PUBLIC_AND_PRIVATE."""

    def test_confirmation_is_still_required(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            ct.kubectl_helpers,
            "describe_cluster_access",
            lambda c, r: dict(ACCESS["PUBLIC_AND_PRIVATE"]),
        )
        created: list[str] = []
        monkeypatch.setattr(
            ct.ephemeral_bastion,
            "create_ephemeral_bastion",
            lambda c, r, **k: created.append(c) or BASTION,
        )
        started: list[str] = []
        monkeypatch.setattr(ct.ssm_tunnel, "start_api_tunnel", lambda *a, **k: started.append("x"))

        def decline(*a: object, **k: object) -> None:
            raise click.exceptions.Abort()

        monkeypatch.setattr(ct, "confirm", decline)
        fmt = _FakeFormatter()
        with (
            pytest.raises(click.exceptions.Abort),
            ct.open_api_server_tunnel(
                fmt, cluster="gco-us-east-1", region="us-east-1", via_ssm=ct.AUTO_BASTION
            ),
        ):
            pytest.fail("body must not run")  # pragma: no cover
        assert created == [] and started == []
        assert any("ephemeral bastion" in m for level, m in fmt.messages if level == "warning")

    def test_the_ttl_reaches_the_bastion(self, monkeypatch: pytest.MonkeyPatch) -> None:
        seams = _Seams(monkeypatch, ACCESS["PUBLIC_AND_PRIVATE"])
        with ct.open_api_server_tunnel(
            _FakeFormatter(),
            cluster="gco-us-east-1",
            region="us-east-1",
            via_ssm=ct.AUTO_BASTION,
            bastion_ttl_minutes=45,
            assume_yes=True,
        ):
            pass
        assert seams.events[0] == "provision:45:True"

    def test_a_failed_tunnel_tears_the_bastion_down(self, monkeypatch: pytest.MonkeyPatch) -> None:
        seams = _Seams(monkeypatch, ACCESS["PUBLIC_AND_PRIVATE"], tunnel_error="tunnel failed")
        with (
            pytest.raises(RuntimeError, match="tunnel failed"),
            ct.open_api_server_tunnel(
                _FakeFormatter(),
                cluster="gco-us-east-1",
                region="us-east-1",
                via_ssm=ct.AUTO_BASTION,
                assume_yes=True,
            ),
        ):
            pytest.fail("body must not run")  # pragma: no cover
        assert seams.events == [
            f"provision:{_TTL}:True",
            f"start:{BASTION}:8443",
            f"teardown:{BASTION}",
        ]

    def test_a_failing_body_still_stops_the_tunnel_and_the_bastion(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        seams = _Seams(monkeypatch, ACCESS["PUBLIC_AND_PRIVATE"])
        with (
            pytest.raises(RuntimeError, match="port-forward failed"),
            ct.open_api_server_tunnel(
                _FakeFormatter(),
                cluster="gco-us-east-1",
                region="us-east-1",
                via_ssm=ct.AUTO_BASTION,
                assume_yes=True,
            ),
        ):
            raise RuntimeError("port-forward failed")
        assert seams.events == [
            f"provision:{_TTL}:True",
            f"start:{BASTION}:8443",
            "stop",
            f"teardown:{BASTION}",
        ]


# ---------------------------------------------------------------------------
# gco cluster tunnel on PUBLIC_AND_PRIVATE (and the explicit-request failures)
# ---------------------------------------------------------------------------


class TestClusterTunnelRoutes:
    def _patch(self, monkeypatch: pytest.MonkeyPatch, access: dict[str, object] | None) -> Any:
        monkeypatch.setattr("cli.kubectl_helpers.update_kubeconfig", lambda c, r: None)

        def describe(c: str, r: str) -> dict[str, object]:
            if access is None:
                raise RuntimeError("describe failed")
            return dict(access)

        monkeypatch.setattr("cli.kubectl_helpers.describe_cluster_access", describe)
        proc = _FakeTunnel()
        monkeypatch.setattr("cli.ssm_tunnel.start_api_tunnel", lambda *a, **k: proc)
        blocked: list[str] = []
        monkeypatch.setattr(
            "cli.commands.cluster_cmd._block_until_interrupt", lambda: blocked.append("held")
        )
        return proc, blocked

    def test_explicit_instance_tunnels_on_public_and_private(
        self, runner: CliRunner, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        proc, blocked = self._patch(monkeypatch, ACCESS["PUBLIC_AND_PRIVATE"])
        res = runner.invoke(
            cli, ["cluster", "tunnel", "--via-ssm", INSTANCE, "--region", "us-east-1"]
        )
        assert res.exit_code == 0, res.output
        assert "takes precedence over its public endpoint" in res.output
        assert "SSM tunnel open" in res.output
        assert "PUBLIC API endpoint" not in res.output
        assert blocked == ["held"]
        assert proc.terminated is True

    def test_auto_provisions_tunnels_and_tears_down_on_public_and_private(
        self, runner: CliRunner, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        proc, blocked = self._patch(monkeypatch, ACCESS["PUBLIC_AND_PRIVATE"])
        lifecycle: list[str] = []
        monkeypatch.setattr(
            "cli.ephemeral_bastion.create_ephemeral_bastion",
            lambda c, r, **k: lifecycle.append("create") or BASTION,
        )
        monkeypatch.setattr(
            "cli.ephemeral_bastion.destroy_ephemeral_bastion",
            lambda iid, r, **k: lifecycle.append(f"destroy:{iid}"),
        )
        res = runner.invoke(
            cli, ["cluster", "tunnel", "--via-ssm", "auto", "-y", "--region", "us-east-1"]
        )
        assert res.exit_code == 0, res.output
        assert lifecycle == ["create", f"destroy:{BASTION}"]
        assert f"Route: SSM tunnel via {BASTION}" in res.output
        assert blocked == ["held"]
        assert proc.terminated is True

    def test_without_via_ssm_public_and_private_stays_direct(
        self, runner: CliRunner, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _proc, blocked = self._patch(monkeypatch, ACCESS["PUBLIC_AND_PRIVATE"])
        res = runner.invoke(cli, ["cluster", "tunnel", "--region", "us-east-1"])
        assert res.exit_code == 0, res.output
        assert "PUBLIC API endpoint" in res.output
        assert "--via-ssm <instance-id> or --via-ssm auto" in res.output
        assert blocked == []

    def test_explicit_instance_on_a_public_only_endpoint_fails_clearly(
        self, runner: CliRunner, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _proc, blocked = self._patch(monkeypatch, ACCESS["PUBLIC"])
        res = runner.invoke(
            cli, ["cluster", "tunnel", "--via-ssm", INSTANCE, "--region", "us-east-1"]
        )
        assert res.exit_code == 1
        assert "endpointPrivateAccess=false" in res.output
        assert "PUBLIC API endpoint" not in res.output
        assert blocked == []

    def test_an_unreadable_access_mode_is_not_reported_as_public(
        self, runner: CliRunner, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _proc, blocked = self._patch(monkeypatch, None)
        res = runner.invoke(cli, ["cluster", "tunnel", "--region", "us-east-1"])
        assert res.exit_code == 0, res.output
        assert "Could not determine endpoint access mode" in res.output
        assert "PUBLIC API endpoint" not in res.output
        assert blocked == []


class TestClusterTunnelPrintRoutes:
    def _invoke(
        self,
        runner: CliRunner,
        monkeypatch: pytest.MonkeyPatch,
        mode: str,
        *extra: str,
        json_output: bool = True,
    ) -> Any:
        monkeypatch.setattr(
            "cli.kubectl_helpers.describe_cluster_access", lambda c, r: dict(ACCESS[mode])
        )
        prefix = ["--output", "json"] if json_output else []
        return runner.invoke(
            cli, [*prefix, "cluster", "tunnel", "--print", "--region", "us-east-1", *extra]
        )

    def test_public_and_private_without_via_ssm_prints_the_direct_plan(
        self, runner: CliRunner, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        res = self._invoke(runner, monkeypatch, "PUBLIC_AND_PRIVATE")
        assert res.exit_code == 0, res.output
        payload = json.loads(res.output)
        assert payload["reachable"] == ct.ROUTE_DIRECT
        assert payload["access_mode"] == "PUBLIC_AND_PRIVATE"
        assert "--via-ssm" in payload["note"]

    def test_public_and_private_with_an_instance_prints_the_tunnel(
        self, runner: CliRunner, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        res = self._invoke(runner, monkeypatch, "PUBLIC_AND_PRIVATE", "--via-ssm", INSTANCE)
        assert res.exit_code == 0, res.output
        payload = json.loads(res.output)
        assert payload["reachable"] == ct.ROUTE_SSM
        assert INSTANCE in payload["ssm_command_str"]

    def test_public_only_with_an_instance_fails_clearly(
        self, runner: CliRunner, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        res = self._invoke(runner, monkeypatch, "PUBLIC", "--via-ssm", INSTANCE)
        assert res.exit_code == 1
        assert "endpointPrivateAccess=false" in res.output

    def test_human_plan_names_the_access_mode(
        self, runner: CliRunner, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        direct = self._invoke(runner, monkeypatch, "PUBLIC_AND_PRIVATE", json_output=False)
        assert direct.exit_code == 0, direct.output
        assert (
            "PUBLIC_AND_PRIVATE endpoint — kubectl reaches the public endpoint directly."
            in direct.output
        )
        tunnel = self._invoke(
            runner, monkeypatch, "PUBLIC_AND_PRIVATE", "--via-ssm", "auto", json_output=False
        )
        assert tunnel.exit_code == 0, tunnel.output
        assert (
            "PUBLIC_AND_PRIVATE endpoint — reach the private endpoint over an SSM tunnel."
            in tunnel.output
        )
        assert "<INSTANCE_ID>" in tunnel.output


# ---------------------------------------------------------------------------
# gco monitoring open on PUBLIC_AND_PRIVATE, and an honest forwarding message
# ---------------------------------------------------------------------------


class TestMonitoringOpenRoutes:
    def _patch(
        self,
        monkeypatch: pytest.MonkeyPatch,
        mode: str,
        captured: dict[str, Any],
        *,
        fail: str | None = None,
    ) -> list[str]:
        monkeypatch.setattr("cli.kubectl_helpers.update_kubeconfig", lambda c, r: None)
        monkeypatch.setattr(
            "cli.kubectl_helpers.describe_cluster_access", lambda c, r: dict(ACCESS[mode])
        )
        lifecycle: list[str] = []
        monkeypatch.setattr(
            "cli.ephemeral_bastion.create_ephemeral_bastion",
            lambda c, r, **k: lifecycle.append("create") or BASTION,
        )
        monkeypatch.setattr(
            "cli.ephemeral_bastion.destroy_ephemeral_bastion",
            lambda iid, r, **k: lifecycle.append(f"destroy:{iid}"),
        )
        proc = _FakeTunnel()

        def start(*a: object, **k: object) -> _FakeTunnel:
            lifecycle.append("tunnel")
            return proc

        monkeypatch.setattr("cli.ssm_tunnel.start_api_tunnel", start)
        monkeypatch.setattr("cli.cluster_ui.exec_port_forward", _ready_forward(captured, fail=fail))
        return lifecycle

    def test_auto_on_public_and_private_forwards_through_the_bastion(
        self, runner: CliRunner, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        captured: dict[str, Any] = {}
        lifecycle = self._patch(monkeypatch, "PUBLIC_AND_PRIVATE", captured)
        res = runner.invoke(
            cli,
            [
                "monitoring",
                "open",
                "--service",
                "mlflow",
                "--via-ssm",
                "auto",
                "-y",
                "--region",
                "us-east-1",
            ],
        )
        assert res.exit_code == 0, res.output
        assert lifecycle == ["create", "tunnel", f"destroy:{BASTION}"]
        assert captured["cmd"][-4:] == [
            "--server",
            "https://127.0.0.1:8443",
            "--tls-server-name",
            PRIVATE_HOST,
        ]
        assert captured["port"] == 5000
        out = res.output
        assert out.index("Route: SSM tunnel") < out.index("Starting kubectl port-forward")
        assert out.index("Starting kubectl port-forward") < out.index(
            "Forwarding mlflow → http://localhost:5000"
        )

    def test_an_instance_on_public_and_private_forwards_through_the_tunnel(
        self, runner: CliRunner, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        captured: dict[str, Any] = {}
        lifecycle = self._patch(monkeypatch, "PUBLIC_AND_PRIVATE", captured)
        res = runner.invoke(
            cli, ["monitoring", "open", "--via-ssm", INSTANCE, "--region", "us-east-1"]
        )
        assert res.exit_code == 0, res.output
        assert lifecycle == ["tunnel"]
        assert "--server" in captured["cmd"]
        assert "Forwarding grafana → http://localhost:3000" in res.output

    def test_without_via_ssm_public_and_private_forwards_directly(
        self, runner: CliRunner, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        captured: dict[str, Any] = {}
        lifecycle = self._patch(monkeypatch, "PUBLIC_AND_PRIVATE", captured)
        res = runner.invoke(cli, ["monitoring", "open", "--region", "us-east-1"])
        assert res.exit_code == 0, res.output
        assert lifecycle == []
        assert "--server" not in captured["cmd"]
        assert "Route: gco-us-east-1's public API endpoint, directly." in res.output

    def test_a_forward_that_never_listens_prints_no_url_and_tears_down(
        self, runner: CliRunner, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        captured: dict[str, Any] = {}
        lifecycle = self._patch(
            monkeypatch,
            "PUBLIC_AND_PRIVATE",
            captured,
            fail="kubectl port-forward exited with code 1 before localhost:5000 was listening",
        )
        res = runner.invoke(
            cli,
            [
                "monitoring",
                "open",
                "--service",
                "mlflow",
                "--via-ssm",
                "auto",
                "-y",
                "--region",
                "us-east-1",
            ],
        )
        assert res.exit_code == 1
        assert "before localhost:5000 was listening" in res.output
        assert "Forwarding mlflow" not in res.output
        assert lifecycle == ["create", "tunnel", f"destroy:{BASTION}"]
