"""KV-cache tooling and split-serving deployability for Mooncake endpoints.

These examples pin the behaviour that makes disaggregated and ``both``-mode
endpoints usable end to end, and the surface for warming their KV cache:

- Reconciliation removes both historical per-endpoint Ingress names. Public
  requests follow the shared ``gco-system/gco-gateway`` ``/inference`` HTTPRoute
  to ``gco-system/inference-proxy``, which authenticates the request before
  forwarding to the endpoint's internal ClusterIP Service.
- A ``store``/``both`` deploy enables the shared KV-cache store by default so
  the store connector is wired to the shared master, and a split deploy
  defaults the prefill-decode proxy image to the endpoint image.
- ``--mooncake-cold-tier`` and the proxy flags thread through to the persisted
  block, and ``populate-kv`` (CLI + MCP) writes into the same cold-tier key
  prefix the per-region monitor reads from, so uploaded objects land where the
  pods look for them.
- The ``populate_kv_cache`` MCP tool uploads host files, so it registers only
  under ``GCO_ENABLE_MODEL_UPLOAD`` and stages a descriptor-backed snapshot of
  a source confined beneath ``GCO_STORAGE_LOCAL_ROOT``.
"""

from __future__ import annotations

import asyncio
import importlib.util
import json
import logging
import os
import sys
from pathlib import Path
from types import ModuleType
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

from cli.inference import InferenceManager
from cli.main import cli
from cli.models import RegionalBucketManager

# Ensure gco_mcp/ is importable, then load the server so every tool is registered.
sys.path.insert(0, str(Path(__file__).parent.parent / "gco_mcp"))

import run_mcp  # import for path/registration side effects

assert run_mcp is not None  # keep the registration import from being pruned

MCP_ROOT = Path(__file__).parent.parent / "gco_mcp"


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
    def test_protocol_and_device_flags_reach_the_manager(self, runner):
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
                    "disaggregated",
                    "-r",
                    "us-east-1",
                    "--mooncake-protocol",
                    "tcp",
                    "--mooncake-device-name",
                    "eth1",
                ],
            )
        assert result.exit_code == 0, result.output
        assert mock_mgr.deploy.call_args.kwargs["mooncake_transfer"] == {
            "protocol": "tcp",
            "device_name": "eth1",
        }

    @pytest.mark.parametrize(
        "flag_args",
        [["--mooncake-protocol", "rdma"], ["--mooncake-device-name", "eth0"]],
    )
    def test_transfer_flags_require_mooncake_mode(self, runner, flag_args):
        mock_mgr = MagicMock()
        with patch("cli.inference.get_inference_manager", return_value=mock_mgr):
            result = runner.invoke(cli, ["inference", "deploy", "pd", *flag_args])
        assert result.exit_code != 0
        assert "require --mooncake-mode" in result.output
        mock_mgr.deploy.assert_not_called()

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


class _RecordingMCP:
    """Stand-in for the FastMCP singleton that records each tool's tags."""

    def __init__(self) -> None:
        self.tags: dict[str, set[str]] = {}

    def tool(self, **kwargs: Any):
        def decorate(function: Any) -> Any:
            self.tags[function.__name__] = set(kwargs.get("tags", ()))
            return function

        return decorate


def _load_inference_tools(*, model_upload: bool) -> tuple[ModuleType, _RecordingMCP]:
    """Import ``tools/inference.py`` against an isolated registry and flag set.

    The real ``cli_runner``, ``audit`` and ``local_data`` modules are used, so
    the audit record, the subprocess argv and the staging are the production
    ones; only the FastMCP singleton and the flag lookup are replaced.
    """
    import feature_flags as real_flags

    flags = ModuleType("feature_flags")
    flags.FLAG_DESTRUCTIVE_OPERATIONS = real_flags.FLAG_DESTRUCTIVE_OPERATIONS  # type: ignore[attr-defined]
    flags.FLAG_MODEL_UPLOAD = real_flags.FLAG_MODEL_UPLOAD  # type: ignore[attr-defined]
    flags.is_enabled = lambda flag: model_upload and flag == real_flags.FLAG_MODEL_UPLOAD  # type: ignore[attr-defined]
    recorder = _RecordingMCP()
    server = ModuleType("server")
    server.mcp = recorder  # type: ignore[attr-defined]
    name = f"_gco_mcp_inference_test_{id(recorder)}"
    spec = importlib.util.spec_from_file_location(name, MCP_ROOT / "tools" / "inference.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    with patch.dict(sys.modules, {"feature_flags": flags, "server": server, name: module}):
        spec.loader.exec_module(module)
    return module, recorder


def _require_dev_fd_directory_access(root: Path) -> None:
    """Skip only when this POSIX host cannot traverse an open directory fd."""
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
    descriptor = os.open(root, flags)
    probe = root / ".dev-fd-probe"
    probe.write_bytes(b"probe")
    try:
        try:
            os.stat(f"/dev/fd/{descriptor}/{probe.name}", follow_symlinks=False)
        except OSError as exc:
            pytest.skip(f"/dev/fd directory traversal is unavailable: {exc}")
    finally:
        probe.unlink(missing_ok=True)
        os.close(descriptor)


class TestPopulateKvCacheTool:
    def test_tool_is_absent_without_the_model_upload_flag(self):
        module, recorder = _load_inference_tools(model_upload=False)

        assert not hasattr(module, "populate_kv_cache")
        assert "populate_kv_cache" not in recorder.tags

    def test_tool_is_gated_and_tagged_like_the_other_upload_tools(self):
        from resources.self import _TOOL_GATING_TABLE

        module, recorder = _load_inference_tools(model_upload=True)

        assert callable(module.populate_kv_cache)
        assert recorder.tags["populate_kv_cache"] == {
            "data-upload",
            "inference",
            "local-filesystem",
        }
        assert _TOOL_GATING_TABLE["populate_kv_cache"] == "GCO_ENABLE_MODEL_UPLOAD"
        assert module.populate_kv_cache.__doc__.startswith("[gated by GCO_ENABLE_MODEL_UPLOAD]")

    def test_tool_uploads_a_confined_snapshot_and_audits(self, caplog, tmp_path):
        module, _ = _load_inference_tools(model_upload=True)
        root = tmp_path / "root"
        source = root / "kv-warm-set"
        source.mkdir(parents=True)
        _require_dev_fd_directory_access(root)
        (source / "block-0.bin").write_bytes(b"kv")
        seen: list[dict[str, Any]] = []

        def fake_run(cmd, **kwargs):
            staged = cmd[cmd.index("populate-kv") + 2]
            seen.append(
                {
                    "cmd": cmd,
                    "pass_fds": kwargs.get("pass_fds"),
                    "timeout": kwargs.get("timeout"),
                    "listing": sorted(os.listdir(staged)),
                }
            )
            return MagicMock(returncode=0, stdout='{"files_uploaded":1}', stderr="")

        with (
            caplog.at_level(logging.INFO, logger="gco.mcp.audit"),
            patch.dict(os.environ, {"GCO_STORAGE_LOCAL_ROOT": str(root)}),
            patch("cli_runner.subprocess.run", side_effect=fake_run),
        ):
            result = asyncio.run(module.populate_kv_cache("llama", "kv-warm-set", "us-east-1"))

        assert result == '{"files_uploaded":1}'
        cmd = seen[0]["cmd"]
        index = cmd.index("populate-kv")
        assert cmd[index - 1] == "inference"
        assert cmd[index + 1] == "llama"
        assert cmd[index + 2].startswith("/dev/fd/")
        assert cmd[index + 2].endswith("/kv-warm-set")
        assert cmd[index + 3 :] == ["-r", "us-east-1"]
        assert seen[0]["listing"] == ["block-0.bin"]
        # An upload gets an hour, not the two-minute default.
        assert seen[0]["timeout"] == 3600
        assert len(seen[0]["pass_fds"]) == 1
        # The private snapshot is removed once the CLI returns.
        assert not list(root.glob(".gco-mcp-upload-*"))
        entries = _audit_entries(caplog)
        assert len(entries) == 1
        assert entries[0]["tool"] == "populate_kv_cache"
        assert entries[0]["status"] == "success"

    def test_tool_hands_the_cli_the_staged_argument_and_its_descriptor(self, tmp_path):
        """Platform-independent check of the staged argv (macOS lacks /dev/fd dirs)."""
        import contextlib

        from local_data import StagedUpload

        module, _ = _load_inference_tools(model_upload=True)
        root = tmp_path / "root"
        root.mkdir()
        (root / "warm.bin").write_bytes(b"kv")
        staged_contracts: list[Any] = []

        @contextlib.contextmanager
        def fake_stage(contract):
            staged_contracts.append(contract)
            yield StagedUpload(argument="/dev/fd/97/warm.bin", directory_fd=97)

        with (
            patch.dict(os.environ, {"GCO_STORAGE_LOCAL_ROOT": str(root)}),
            patch.object(module, "stage_upload_path", fake_stage),
            patch.object(module.cli_runner, "_run_cli", return_value="{}") as run_cli,
        ):
            assert asyncio.run(module.populate_kv_cache("llama", "warm.bin", "us-west-2")) == "{}"

        run_cli.assert_called_once_with(
            "inference",
            "populate-kv",
            "llama",
            "/dev/fd/97/warm.bin",
            "-r",
            "us-west-2",
            timeout_seconds=module.cli_runner.TRANSFER_TIMEOUT_SECONDS,
            pass_fds=(97,),
        )
        assert module.cli_runner.TRANSFER_TIMEOUT_SECONDS == 3600
        assert staged_contracts[0].resolved_path == (root / "warm.bin").resolve()

    def test_tool_refuses_a_path_outside_the_local_root(self, tmp_path):
        module, _ = _load_inference_tools(model_upload=True)
        root = tmp_path / "root"
        root.mkdir()
        outside = tmp_path / "secrets"
        outside.mkdir()

        with (
            patch.dict(os.environ, {"GCO_STORAGE_LOCAL_ROOT": str(root)}),
            patch("cli_runner.subprocess.run") as mock_run,
        ):
            absolute = asyncio.run(module.populate_kv_cache("llama", str(outside), "us-east-1"))
            relative = asyncio.run(module.populate_kv_cache("llama", "../secrets", "us-east-1"))

        for result in (absolute, relative):
            payload = json.loads(result)
            assert payload["code"] == "local_data_path_rejected"
            assert "GCO_STORAGE_LOCAL_ROOT" in payload["error"]
        mock_run.assert_not_called()

    def test_tool_requires_a_configured_local_root(self):
        module, _ = _load_inference_tools(model_upload=True)

        with (
            patch.dict(os.environ, {"GCO_STORAGE_LOCAL_ROOT": ""}),
            patch("cli_runner.subprocess.run") as mock_run,
        ):
            result = asyncio.run(module.populate_kv_cache("llama", "./data", "us-east-1"))

        assert json.loads(result)["code"] == "local_data_path_rejected"
        mock_run.assert_not_called()

    def test_tool_rejects_a_snapshot_that_cannot_be_staged(self, tmp_path):
        module, _ = _load_inference_tools(model_upload=True)
        root = tmp_path / "root"
        source = root / "kv-warm-set"
        source.mkdir(parents=True)
        outside = tmp_path / "outside.bin"
        outside.write_bytes(b"secret")
        (source / "escape.bin").symlink_to(outside)

        with (
            patch.dict(os.environ, {"GCO_STORAGE_LOCAL_ROOT": str(root)}),
            patch("cli_runner.subprocess.run") as mock_run,
        ):
            result = asyncio.run(module.populate_kv_cache("llama", "kv-warm-set", "us-east-1"))

        payload = json.loads(result)
        assert payload["code"] == "local_data_path_rejected"
        assert "symbolic link" in payload["error"]
        mock_run.assert_not_called()
        assert not list(root.glob(".gco-mcp-upload-*"))

    def test_tool_records_non_zero_exit(self, caplog, tmp_path):
        module, _ = _load_inference_tools(model_upload=True)
        root = tmp_path / "root"
        root.mkdir()
        _require_dev_fd_directory_access(root)
        (root / "d.bin").write_bytes(b"kv")

        with (
            caplog.at_level(logging.INFO, logger="gco.mcp.audit"),
            patch.dict(os.environ, {"GCO_STORAGE_LOCAL_ROOT": str(root)}),
            patch("cli_runner.subprocess.run") as mock_run,
        ):
            mock_run.return_value = MagicMock(
                returncode=1, stdout="", stderr="Regional bucket not found"
            )
            result = asyncio.run(module.populate_kv_cache("ghost", "d.bin", "us-east-1"))

        payload = json.loads(result)
        assert payload["exit_code"] == 1
        assert "Regional bucket not found" in payload["error"]
        entries = _audit_entries(caplog)
        assert len(entries) == 1
        assert entries[0]["tool"] == "populate_kv_cache"
