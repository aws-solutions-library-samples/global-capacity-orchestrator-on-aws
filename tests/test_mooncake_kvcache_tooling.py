"""KV-cache tooling and split-serving deployability for Mooncake endpoints.

These examples pin the behaviour that makes disaggregated and ``both``-mode
endpoints usable end to end, and the surface for warming their KV cache:

- The public prefill-decode proxy Ingress is scoped to the endpoint's own
  ingress prefix (``/inference/{name}/v1``), so a client request — which always
  arrives at ``/inference/{name}/...`` through Global Accelerator and the shared
  ALB — actually reaches the proxy and stays isolated from other endpoints on
  that ALB. A bare ``/v1`` rule would match neither the client URL nor stay
  endpoint-scoped.
- A ``store``/``both`` deploy enables the shared KV-cache store by default so
  the store connector is wired to the shared master, and a split deploy
  defaults the prefill-decode proxy image to the endpoint image.
- ``--mooncake-cold-tier`` and the proxy flags thread through to the persisted
  block, and ``populate-kv`` (CLI + MCP) writes into the same cold-tier key
  prefix the per-region monitor reads from, so uploaded objects land where the
  pods look for them.
"""

from __future__ import annotations

import asyncio
import json
import logging
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

from cli.inference import InferenceManager
from cli.main import cli
from cli.models import RegionalBucketManager

# Ensure mcp/ is importable, then load the server so every tool is registered.
sys.path.insert(0, str(Path(__file__).parent.parent / "mcp"))

import run_mcp  # noqa: E402  (import for path/registration side effects)
from tools.inference import populate_kv_cache  # noqa: E402

assert run_mcp is not None  # keep the registration import from being pruned


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_manager() -> InferenceManager:
    """Build an InferenceManager without touching AWS in the constructor."""
    with patch("cli.inference.get_aws_client", return_value=MagicMock()):
        return InferenceManager(config=MagicMock())


def _make_monitor(region: str = "us-east-1"):
    """Build an InferenceMonitor with every Kubernetes client mocked out."""
    with (
        patch("gco.services.inference_monitor.config.load_incluster_config"),
        patch("gco.services.inference_monitor.client.AppsV1Api") as mock_apps,
        patch("gco.services.inference_monitor.client.CoreV1Api") as mock_core,
        patch("gco.services.inference_monitor.client.NetworkingV1Api") as mock_net,
        patch("gco.services.inference_monitor.client.AutoscalingV2Api"),
    ):
        from gco.services.inference_monitor import InferenceMonitor

        monitor = InferenceMonitor(
            cluster_id="test-cluster",
            region=region,
            store=MagicMock(),
            namespace="gco-inference",
            reconcile_interval=5,
        )
        monitor.apps_v1 = mock_apps.return_value
        monitor.core_v1 = mock_core.return_value
        monitor.networking_v1 = mock_net.return_value
        return monitor


def _audit_entries(caplog) -> list[dict]:
    """Pull the JSON audit entries out of caplog, in order."""
    return [
        json.loads(record.message) for record in caplog.records if record.name == "gco.mcp.audit"
    ]


@pytest.fixture
def runner():
    return CliRunner()


@pytest.fixture(autouse=True)
def mock_config():
    """Patch config loading so CLI runs never read a real cdk.json."""
    mock_cfg = MagicMock()
    mock_cfg.output_format = "table"
    mock_cfg.global_region = "us-east-2"
    mock_cfg.project_name = "gco"
    with patch("cli.main.get_config", return_value=mock_cfg):
        yield mock_cfg


def _ingress_paths(monitor) -> list[str]:
    """Return the routing paths from the last created proxy Ingress."""
    args, _ = monitor.networking_v1.create_namespaced_ingress.call_args
    ingress = args[1]
    return [p.path for rule in ingress.spec.rules for p in rule.http.paths]


# ===========================================================================
# Invoke path: the proxy Ingress is scoped to the endpoint's ingress prefix
# ===========================================================================


class TestProxyIngressScoping:
    def test_published_path_matches_the_client_invoke_url(self):
        """The published prefix is a prefix of the URL the CLI builds and sends.

        ``gco inference invoke`` posts to ``/inference/{name}{api_path}``. The
        proxy Ingress must publish ``/inference/{name}/v1`` so that request
        routes to the proxy rather than missing every rule on the shared ALB.
        """
        monitor = _make_monitor()
        monitor._update_proxy_ingress("llama", "llama-proxy", "gco-inference", {})

        paths = _ingress_paths(monitor)
        assert paths == ["/inference/llama/v1"]
        client_invoke_url = "/inference/llama/v1/completions"
        assert client_invoke_url.startswith(paths[0])

    def test_respects_a_custom_ingress_path(self):
        """A non-default ingress path on the endpoint is honoured for the proxy."""
        monitor = _make_monitor()
        monitor._update_proxy_ingress(
            "llama", "llama-proxy", "gco-inference", {"ingress_path": "/custom/llama"}
        )
        assert _ingress_paths(monitor) == ["/custom/llama/v1"]

    def test_admin_path_is_never_published(self):
        """The privileged ``/instances/add`` admin path stays off the public surface."""
        monitor = _make_monitor()
        monitor._update_proxy_ingress("llama", "llama-proxy", "gco-inference", {})
        paths = _ingress_paths(monitor)
        assert all("/instances" not in p for p in paths)

    def test_healthcheck_path_is_scoped_too(self):
        """The ALB health-check path tracks the scoped serving prefix."""
        monitor = _make_monitor()
        monitor._update_proxy_ingress("llama", "llama-proxy", "gco-inference", {})
        args, _ = monitor.networking_v1.create_namespaced_ingress.call_args
        ingress = args[1]
        hc = ingress.metadata.annotations["alb.ingress.kubernetes.io/healthcheck-path"]
        assert hc == "/inference/llama/v1"


# ===========================================================================
# Split-serving deployability: store auto-enable and proxy image default
# ===========================================================================


class TestSplitDeployDefaults:
    def _deploy_spec(self, **kwargs) -> dict:
        """Run deploy with a mocked store and return the persisted spec."""
        mock_store = MagicMock()
        mock_store.create_endpoint.return_value = {"endpoint_name": "ep"}
        mgr = _make_manager()
        with (
            patch.object(mgr, "_get_store", return_value=mock_store),
            patch(
                "cli.images.default_disaggregated_image",
                return_value="vllm/vllm-openai:test",
            ),
        ):
            mgr.deploy(
                "ep",
                image=None,
                target_regions=["us-east-1"],
                rewrite_image=False,
                **kwargs,
            )
        return mock_store.create_endpoint.call_args.kwargs["spec"]

    def test_both_mode_enables_the_store_by_default(self):
        spec = self._deploy_spec(mooncake_mode="both", prefill_replicas=1, decode_replicas=1)
        assert spec["mooncake"]["store"]["enabled"] is True

    def test_store_mode_enables_the_store_by_default(self):
        spec = self._deploy_spec(mooncake_mode="store")
        assert spec["mooncake"]["store"]["enabled"] is True

    def test_disaggregated_mode_does_not_add_a_store(self):
        spec = self._deploy_spec(
            mooncake_mode="disaggregated", prefill_replicas=1, decode_replicas=1
        )
        assert "store" not in spec["mooncake"]

    def test_split_mode_defaults_proxy_image_to_endpoint_image(self):
        spec = self._deploy_spec(
            mooncake_mode="disaggregated", prefill_replicas=2, decode_replicas=2
        )
        assert spec["mooncake"]["proxy"]["image"] == "vllm/vllm-openai:test"
        assert spec["image"] == "vllm/vllm-openai:test"

    def test_explicit_proxy_image_is_preserved(self):
        mock_store = MagicMock()
        mock_store.create_endpoint.return_value = {"endpoint_name": "ep"}
        mgr = _make_manager()
        with (
            patch.object(mgr, "_get_store", return_value=mock_store),
            patch(
                "cli.images.default_disaggregated_image",
                return_value="vllm/vllm-openai:test",
            ),
        ):
            mgr.deploy(
                "ep",
                image=None,
                target_regions=["us-east-1"],
                rewrite_image=False,
                mooncake_mode="both",
                prefill_replicas=1,
                decode_replicas=1,
                mooncake_proxy={"image": "my/proxy:v1", "admin_api_key_secret": "s"},
            )
        spec = mock_store.create_endpoint.call_args.kwargs["spec"]
        assert spec["mooncake"]["proxy"]["image"] == "my/proxy:v1"
        assert spec["mooncake"]["proxy"]["admin_api_key_secret"] == "s"

    def test_cold_tier_store_block_round_trips(self):
        spec = self._deploy_spec(
            mooncake_mode="both",
            prefill_replicas=1,
            decode_replicas=1,
            mooncake_store={"enabled": True, "cold_tier_enabled": True},
        )
        assert spec["mooncake"]["store"]["cold_tier_enabled"] is True
        assert spec["mooncake"]["store"]["enabled"] is True


# ===========================================================================
# CLI deploy: the dedicated mooncake flags thread through to the manager
# ===========================================================================


class TestDeployFlagThreading:
    def test_cold_tier_and_proxy_flags_reach_the_manager(self, runner):
        mock_mgr = MagicMock()
        mock_mgr.deploy.return_value = {
            "endpoint_name": "pd",
            "target_regions": ["us-east-1"],
            "ingress_path": "/inference/pd",
        }
        mock_aws = MagicMock()
        mock_aws.discover_regional_stacks.return_value = {"us-east-1": {}}
        with (
            patch("cli.inference.get_inference_manager", return_value=mock_mgr),
            patch("cli.aws_client.get_aws_client", return_value=mock_aws),
        ):
            result = runner.invoke(
                cli,
                [
                    "inference",
                    "deploy",
                    "pd",
                    "--mooncake-mode",
                    "both",
                    "-r",
                    "us-east-1",
                    "--mooncake-cold-tier",
                    "--mooncake-admin-key-secret",
                    "pd-admin",
                ],
            )
        assert result.exit_code == 0, result.output
        kwargs = mock_mgr.deploy.call_args.kwargs
        assert kwargs["mooncake_store"] == {"enabled": True, "cold_tier_enabled": True}
        assert kwargs["mooncake_proxy"] == {"admin_api_key_secret": "pd-admin"}

    def test_cold_tier_rejected_for_disaggregated_mode(self, runner):
        mock_mgr = MagicMock()
        with patch("cli.inference.get_inference_manager", return_value=mock_mgr):
            result = runner.invoke(
                cli,
                [
                    "inference",
                    "deploy",
                    "pd",
                    "--mooncake-mode",
                    "disaggregated",
                    "-r",
                    "us-east-1",
                    "--mooncake-cold-tier",
                ],
            )
        assert result.exit_code != 0
        assert "cold-tier requires" in result.output
        mock_mgr.deploy.assert_not_called()

    def test_split_deploy_without_admin_secret_warns(self, runner):
        mock_mgr = MagicMock()
        mock_mgr.deploy.return_value = {
            "endpoint_name": "pd",
            "target_regions": ["us-east-1"],
            "ingress_path": "/inference/pd",
        }
        mock_aws = MagicMock()
        mock_aws.discover_regional_stacks.return_value = {"us-east-1": {}}
        with (
            patch("cli.inference.get_inference_manager", return_value=mock_mgr),
            patch("cli.aws_client.get_aws_client", return_value=mock_aws),
        ):
            result = runner.invoke(
                cli,
                ["inference", "deploy", "pd", "--mooncake-mode", "both", "-r", "us-east-1"],
            )
        assert result.exit_code == 0, result.output
        assert "admin-key-secret" in result.output


# ===========================================================================
# CLI configure-store: merges onto the current store block
# ===========================================================================


class TestConfigureStoreCommand:
    def test_cold_tier_merges_and_keeps_existing_fields(self, runner):
        mock_mgr = MagicMock()
        mock_mgr.get_endpoint.return_value = {
            "endpoint_name": "ep",
            "spec": {"mooncake": {"mode": "both", "store": {"enabled": True, "offload": "cpu"}}},
        }
        mock_mgr.configure_store.return_value = {"endpoint_name": "ep"}
        with patch("cli.inference.get_inference_manager", return_value=mock_mgr):
            result = runner.invoke(cli, ["inference", "configure-store", "ep", "--cold-tier"])
        assert result.exit_code == 0, result.output
        store_arg = mock_mgr.configure_store.call_args.args[1]
        # Enabling the cold tier sets the flag and keeps the existing offload.
        assert store_arg["cold_tier_enabled"] is True
        assert store_arg["enabled"] is True
        assert store_arg["offload"] == "cpu"

    def test_missing_endpoint_reports_and_does_not_configure(self, runner):
        mock_mgr = MagicMock()
        mock_mgr.get_endpoint.return_value = None
        with patch("cli.inference.get_inference_manager", return_value=mock_mgr):
            result = runner.invoke(cli, ["inference", "configure-store", "ghost", "--cold-tier"])
        assert result.exit_code != 0
        assert "not found" in result.output
        mock_mgr.configure_store.assert_not_called()


# ===========================================================================
# populate-kv: writes into the cold-tier key prefix the monitor reads from
# ===========================================================================


class TestPopulateKvCache:
    def test_uploads_under_the_cold_tier_prefix(self):
        from gco.stacks.constants import MOONCAKE_COLD_TIER_KEY_PREFIX

        mgr = RegionalBucketManager(config=MagicMock())
        with patch.object(
            mgr,
            "upload",
            return_value={
                "region": "us-east-1",
                "bucket": "b",
                "s3_uri": "s3://b/x",
                "files_uploaded": 3,
            },
        ) as upload:
            result = mgr.populate_kv_cache("./data", "us-east-1", "llama")

        upload.assert_called_once()
        assert upload.call_args.args[0] == "./data"
        assert upload.call_args.args[1] == "us-east-1"
        assert upload.call_args.kwargs["prefix"] == f"{MOONCAKE_COLD_TIER_KEY_PREFIX}/llama"
        assert result["endpoint"] == "llama"

    def test_cli_populate_kv_shells_through_the_manager(self, runner):
        mock_mgr = MagicMock()
        mock_mgr.populate_kv_cache.return_value = {
            "region": "us-east-1",
            "bucket": "b",
            "s3_uri": "s3://b/mooncake-kv/llama",
            "files_uploaded": 2,
            "endpoint": "llama",
        }
        with patch("cli.models.get_regional_bucket_manager", return_value=mock_mgr):
            result = runner.invoke(
                cli,
                ["inference", "populate-kv", "llama", "./data", "-r", "us-east-1"],
            )
        assert result.exit_code == 0, result.output
        mock_mgr.populate_kv_cache.assert_called_once_with("./data", "us-east-1", "llama")


class TestPopulateKvCacheTool:
    def test_tool_shells_populate_kv_and_audits(self, caplog):
        with (
            caplog.at_level(logging.INFO, logger="gco.mcp.audit"),
            patch("cli_runner.subprocess.run") as mock_run,
        ):
            mock_run.return_value = MagicMock(
                returncode=0, stdout='{"files_uploaded":2}', stderr=""
            )
            result = asyncio.run(populate_kv_cache("llama", "./data", "us-east-1"))

        assert result == '{"files_uploaded":2}'
        cmd = mock_run.call_args[0][0]
        assert "inference" in cmd
        assert "populate-kv" in cmd
        assert "llama" in cmd
        assert "./data" in cmd
        assert "-r" in cmd
        assert "us-east-1" in cmd

        entries = _audit_entries(caplog)
        assert len(entries) == 1
        assert entries[0]["tool"] == "populate_kv_cache"
        assert entries[0]["status"] == "success"

    def test_tool_records_non_zero_exit(self, caplog):
        with (
            caplog.at_level(logging.INFO, logger="gco.mcp.audit"),
            patch("cli_runner.subprocess.run") as mock_run,
        ):
            mock_run.return_value = MagicMock(
                returncode=1, stdout="", stderr="Regional bucket not found"
            )
            result = asyncio.run(populate_kv_cache("ghost", "./d", "us-east-1"))

        payload = json.loads(result)
        assert payload["exit_code"] == 1
        assert "Regional bucket not found" in payload["error"]

        entries = _audit_entries(caplog)
        assert len(entries) == 1
        assert entries[0]["tool"] == "populate_kv_cache"
