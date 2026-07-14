"""
Tests for the GCO MCP server (gco_mcp/run_mcp.py).

Drives the thin _run_cli wrapper that subprocesses the `gco` CLI with
--output json, covering success, empty-stdout → {"status":"ok"}, non-zero
exits mapped to {"error":..., "exit_code":...}, the stderr/stdout
fallback, TimeoutExpired surfacing, FileNotFoundError when gco isn't on
PATH, and cwd pinning to PROJECT_ROOT. Also asserts tool registration —
base tool count, gated reserve_capacity registration when
GCO_ENABLE_CAPACITY_PURCHASE=true, and expected tool name set —
without mocking the FastMCP server itself.
"""

import asyncio
import json
import logging
import os
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# Ensure gco_mcp/ is importable
sys.path.insert(0, str(Path(__file__).parent.parent / "gco_mcp"))

import cli_runner
import run_mcp


class TestRunCli:
    """Tests for the _run_cli helper function."""

    def test_successful_command(self):
        with patch("cli_runner.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout='{"jobs": []}', stderr="")
            result = cli_runner._run_cli("jobs", "list")
            assert result == '{"jobs": []}'
            mock_run.assert_called_once()
            cmd = mock_run.call_args[0][0]
            assert os.path.basename(cmd[0]) == "gco"
            assert "--output" in cmd
            assert "json" in cmd

    def test_empty_stdout_returns_ok(self):
        with patch("cli_runner.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
            result = cli_runner._run_cli("stacks", "list")
            assert json.loads(result) == {"status": "ok"}

    def test_nonzero_exit_returns_error(self):
        with patch("cli_runner.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=1, stdout="", stderr="Stack not found")
            result = cli_runner._run_cli("stacks", "status", "bad-stack")
            parsed = json.loads(result)
            assert parsed["error"] == "Stack not found"
            assert parsed["exit_code"] == 1

    def test_nonzero_exit_falls_back_to_stdout(self):
        with patch("cli_runner.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=1, stdout="some output", stderr="")
            result = cli_runner._run_cli("jobs", "get", "missing")
            parsed = json.loads(result)
            assert parsed["error"] == "some output"

    def test_timeout_returns_error(self):
        with patch("cli_runner.subprocess.run") as mock_run:
            import subprocess

            mock_run.side_effect = subprocess.TimeoutExpired(
                cmd="gco", timeout=120
            )  # nosemgrep: dangerous-subprocess-use-audit - test fixture: mocking TimeoutExpired with static string, not a real subprocess call
            result = cli_runner._run_cli("stacks", "deploy-all")
            parsed = json.loads(result)
            assert "timed out" in parsed["error"].lower()

    def test_cli_not_found_returns_error(self):
        with patch("cli_runner.subprocess.run") as mock_run:
            mock_run.side_effect = FileNotFoundError()
            result = cli_runner._run_cli("jobs", "list")
            parsed = json.loads(result)
            assert "not found" in parsed["error"].lower()

    def test_passes_cwd_as_project_root(self):
        with patch("cli_runner.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout="ok", stderr="")
            cli_runner._run_cli("stacks", "list")
            assert mock_run.call_args[1]["cwd"] == str(cli_runner.PROJECT_ROOT)


class TestServerMetadata:
    """Guard that server-level metadata stays in sync with the project."""

    def test_mcp_server_version_matches_project_version(self):
        """The MCP server must report the same version string as the project.

        ``_MCP_SERVER_VERSION`` feeds the startup audit log and any future
        ``server_info`` surface; hard-coding it would silently drift from the
        canonical ``VERSION`` file that the rest of the tree tracks via
        ``scripts/bump_version.py``. Keeping the assertion tight here makes
        the drift loud at test time.
        """
        # Import from the gco package the same way the MCP server does,
        # so this test fails if the import chain ever breaks (e.g. someone
        # deletes gco/_version.py and leaves MCP hard-coding a literal).
        from gco._version import __version__ as project_version

        assert project_version == run_mcp._PROJECT_VERSION
        assert project_version == run_mcp._MCP_SERVER_VERSION

    def test_mcp_server_version_matches_version_file(self):
        """End-to-end: ``VERSION`` → ``gco/_version`` → ``run_mcp`` should agree.

        This catches the case where ``gco/_version.py`` hasn't been re-synced
        from ``VERSION`` (e.g. ``bump_version.py`` only wrote to ``VERSION``
        and the Python mirror was forgotten).
        """
        version_file = cli_runner.PROJECT_ROOT / "VERSION"
        if not version_file.is_file():
            import pytest

            pytest.skip("VERSION file missing from repo root")
        expected = version_file.read_text().strip()
        assert expected == run_mcp._MCP_SERVER_VERSION, (
            f"MCP server reports {run_mcp._MCP_SERVER_VERSION!r} but VERSION "
            f"file contains {expected!r} — run scripts/bump_version.py to resync."
        )


class TestToolRegistration:
    """Verify tools are registered with correct signatures.

    Uses ``mcp._list_tools()`` to bypass the BM25/Code-Mode catalog-replacement
    transforms wired in ``gco_mcp/server.py``. The public ``list_tools()`` is what
    clients see — the BM25 transform replaces it with a synthetic
    ``search_tools``/``call_tool`` pair plus the always-visible entry-points,
    so testing registration against it would only ever see ~5 tools regardless
    of what's registered. The underlying registry is what we actually want to
    assert against here.
    """

    def test_tool_count(self):
        tools = asyncio.run(run_mcp.mcp._list_tools())
        # 102 base tools after delete_job and delete_inference moved under
        # GCO_ENABLE_DESTRUCTIVE_OPERATIONS and the three default-on metric
        # readers were added, plus five unconditional inference/storage tools
        # and the two read-only image-mirror tools. The breakdown:
        #   * the original 81 (read-only + low-risk + discovery) minus 2
        #     (delete_job + delete_inference) = 79
        #   * 13 unconditional image-registry tools (read-only + administrative,
        #     including images_mirror_plan / images_mirror_status)
        #   * 2 unconditional task observability tools (task_status, task_tail)
        #   * 3 unconditional, default-on metric readers (metrics_cloudwatch_get,
        #     metrics_from_job_logs, metrics_from_shared_storage_file)
        #   * 5 unconditional, low-risk inference/storage tools
        #     (deploy_disaggregated_inference, set_mooncake_topology,
        #     mooncake_topology_status, populate_kv_cache,
        #     upload_to_regional_bucket)
        #   * 2 unconditional ODCR/Capacity-Block parity tools
        #     (find_capacity_reservations sweep, nodepools_create_capacity_block)
        # = 104 total at default registration.
        # reserve_capacity and create_reservation add 2 when
        # GCO_ENABLE_CAPACITY_PURCHASE=true.
        # Image-publish-gated tools (images_build, images_push, images_mirror)
        # add 3 when GCO_ENABLE_IMAGE_PUBLISH=true. Destructive-gated tools add
        # 14 when GCO_ENABLE_DESTRUCTIVE_OPERATIONS=true: delete_job,
        # delete_inference, delete_template, delete_webhook, delete_model,
        # delete_nodepool, analytics_user_remove, monitoring_user_remove,
        # cancel_queue_job,
        # cancel_reservation (ten non-image), plus images_cleanup, images_prune,
        # images_delete_tag, images_delete_repo (four image variants). Model-upload-gated
        # models_upload adds 1 when GCO_ENABLE_MODEL_UPLOAD=true.
        # Infrastructure-deploy gated tools (deploy_stack, deploy_all,
        # bootstrap_cdk) add 3 when GCO_ENABLE_INFRASTRUCTURE_DEPLOY=true.
        # Infrastructure-destroy gated tools (destroy_stack, destroy_all)
        # add 2 when GCO_ENABLE_INFRASTRUCTURE_DESTROY=true.
        # The local-file metric reader (metrics_from_local_file) adds 1 when
        # GCO_ENABLE_LOCAL_METRICS=true. The semantic-progress judge
        # (metrics_semantic_progress) adds 1 when GCO_ENABLE_SEMANTIC_PROGRESS=true.
        # The nine mission_* tools (mission_start, mission_status, mission_iterate,
        # mission_checkpoint, mission_complete, mission_abort, mission_resume,
        # mission_history, mission_list) add 9 when GCO_ENABLE_MISSION=true.
        # Five unconditional cluster-observability tools (monitoring_status,
        # monitoring_users_list, enable_monitoring, disable_monitoring,
        # monitoring_user_add) bring the base to 114.
        # With every flag enabled the ceiling is
        # 114 + 2 + 3 + 14 + 1 + 3 + 2 + 1 + 1 + 9 = 150.
        # (114 base includes the unconditional find_capacity_blocks and
        # find_capacity_reservations sweep tools plus the
        # nodepools_create_capacity_block generator.)
        base_count = 114
        tool_names = [t.name for t in tools]
        expected = base_count
        if "reserve_capacity" in tool_names:
            # reserve_capacity + create_reservation register together under
            # GCO_ENABLE_CAPACITY_PURCHASE.
            expected += 2
        if "images_build" in tool_names:
            expected += 2  # images_build + images_push register together
        if "images_mirror" in tool_names:
            expected += 1  # images_mirror (also GCO_ENABLE_IMAGE_PUBLISH-gated)
        if "delete_job" in tool_names:
            # The destructive-gated non-image tools register together with the
            # four destructive image variants. Adding monitoring_user_remove
            # brings the set to fourteen under the flag.
            expected += 14
        if "models_upload" in tool_names:
            expected += 1
        if "deploy_stack" in tool_names:
            expected += 3  # deploy_stack + deploy_all + bootstrap_cdk
        if "destroy_stack" in tool_names:
            expected += 2  # destroy_stack + destroy_all
        if "metrics_from_local_file" in tool_names:
            expected += 1  # gated by GCO_ENABLE_LOCAL_METRICS
        if "metrics_semantic_progress" in tool_names:
            expected += 1  # gated by GCO_ENABLE_SEMANTIC_PROGRESS
        if "mission_start" in tool_names:
            # The nine mission_* tools register together under GCO_ENABLE_MISSION.
            expected += 9
        assert len(tools) == expected

    def test_all_tool_names(self):
        tools = asyncio.run(run_mcp.mcp._list_tools())
        names = {t.name for t in tools}
        expected = {
            # ── Job management ──
            # Read-only
            "list_jobs",
            "get_job",
            "get_job_logs",
            "get_job_events",
            "cluster_health",
            "queue_status",
            # Mutating
            "submit_job_sqs",
            "submit_job_api",
            # ── Capacity (all read-only) ──
            "check_capacity",
            "capacity_status",
            "recommend_region",
            "spot_prices",
            "ai_recommend",
            "list_reservations",
            "reservation_check",
            "find_capacity_blocks",
            "find_capacity_reservations",
            "capacity_history_show",
            "capacity_history_stats",
            "capacity_history_patterns",
            "capacity_predict",
            # ── Inference endpoints ──
            # Read-only
            "list_inference_endpoints",
            "inference_status",
            "inference_health",
            "list_endpoint_models",
            "invoke_inference",
            "chat_inference",
            # Mutating
            "deploy_inference",
            "scale_inference",
            "update_inference_image",
            "stop_inference",
            "start_inference",
            "canary_deploy",
            "promote_canary",
            "rollback_canary",
            # Disaggregated (Mooncake) inference — low-risk
            "deploy_disaggregated_inference",
            "set_mooncake_topology",
            "mooncake_topology_status",
            "populate_kv_cache",
            # ── Cost tracking (all read-only) ──
            "cost_summary",
            "cost_by_region",
            "cost_trend",
            "cost_forecast",
            # ── Infrastructure / stacks ──
            # Read-only
            "list_stacks",
            "stack_status",
            "fsx_status",
            # Mutating
            "setup_cluster_access",
            # ── Storage (all read-only) ──
            "list_storage_contents",
            "list_file_systems",
            # ── Model weights (all read-only) ──
            "list_models",
            "get_model_uri",
            # ── Metrics (read-only, default-on) ──
            "metrics_cloudwatch_get",
            "metrics_from_job_logs",
            "metrics_from_shared_storage_file",
            #
            # Async tools (all read-only, "safe" risk tier)
            #
            # Stacks inspection
            "stack_diff",
            "stack_outputs",
            "stack_synth",
            "valkey_status",
            "aurora_status",
            # Stacks mutating (cdk.json toggles)
            "enable_fsx",
            "disable_fsx",
            "enable_valkey",
            "disable_valkey",
            "enable_aurora",
            "disable_aurora",
            # Queue
            "queue_list",
            "queue_get",
            "queue_stats",
            # Mutating
            "queue_submit",
            # Templates
            "templates_list",
            "templates_get",
            # Mutating
            "templates_create",
            "templates_run",
            # Webhooks
            "webhooks_list",
            "webhooks_get",
            # Mutating
            "webhooks_create",
            # DAG
            "dag_validate",
            # Mutating
            "dag_run",
            # NodePools
            "nodepools_list",
            "nodepools_describe",
            # Mutating
            "nodepools_create_odcr",
            "nodepools_create_capacity_block",
            # Analytics
            "analytics_doctor",
            "analytics_login_url",
            "analytics_users_list",
            # Mutating
            "enable_analytics",
            "disable_analytics",
            "analytics_user_add",
            # Cluster observability (monitoring)
            # Read-only
            "monitoring_status",
            "monitoring_users_list",
            # Mutating
            "enable_monitoring",
            "disable_monitoring",
            "monitoring_user_add",
            # Config
            "config_get",
            # Examples discovery
            "find_examples",
            # Docs discovery
            "find_docs",
            # Storage (read-only)
            "files_get",
            "files_access_points",
            # Storage (low-risk regional upload)
            "upload_to_regional_bucket",
            # ── Image registry ──
            # Read-only ("safe" risk tier)
            "images_list",
            "images_tags",
            "images_describe",
            "images_uri",
            "images_replication_get",
            "images_replication_status",
            "images_orphans",
            # Image mirror (read-only "safe")
            "images_mirror_plan",
            "images_mirror_status",
            # Administrative ("low-risk")
            "images_init",
            "images_lifecycle_get",
            "images_lifecycle_set",
            "images_replication_sync",
            # ── Long-running task observability (read-only) ──
            "task_status",
            "task_tail",
        }
        # reserve_capacity + create_reservation are conditionally registered via
        # env var and may also appear if a prior test reloaded the module
        if "reserve_capacity" in names:
            expected.add("reserve_capacity")
            expected.add("create_reservation")
        # Image-publish-gated tools register under GCO_ENABLE_IMAGE_PUBLISH.
        if "images_build" in names:
            expected.add("images_build")
            expected.add("images_push")
        # images_mirror is also GCO_ENABLE_IMAGE_PUBLISH-gated but registers
        # independently of the Progress-injected build/push pair.
        if "images_mirror" in names:
            expected.add("images_mirror")
        # Destructive-gated image tools register under
        # GCO_ENABLE_DESTRUCTIVE_OPERATIONS.
        if "images_delete_tag" in names:
            expected.update(
                {
                    "images_cleanup",
                    "images_prune",
                    "images_delete_tag",
                    "images_delete_repo",
                }
            )
        # Destructive-gated non-image tools also register under
        # GCO_ENABLE_DESTRUCTIVE_OPERATIONS — delete_job and delete_inference
        # moved here in the destructive-flag migration, joined by the new
        # delete_template/delete_webhook/delete_model/delete_nodepool/
        # analytics_user_remove/cancel_queue_job tools.
        if "delete_job" in names:
            expected.update(
                {
                    "delete_job",
                    "delete_inference",
                    "delete_template",
                    "delete_webhook",
                    "delete_model",
                    "delete_nodepool",
                    "analytics_user_remove",
                    "monitoring_user_remove",
                    "cancel_queue_job",
                    "cancel_reservation",
                }
            )
        # Model-upload gated tool registers under GCO_ENABLE_MODEL_UPLOAD.
        if "models_upload" in names:
            expected.add("models_upload")
        # Infrastructure-deploy gated tools register under
        # GCO_ENABLE_INFRASTRUCTURE_DEPLOY.
        if "deploy_stack" in names:
            expected.update({"deploy_stack", "deploy_all", "bootstrap_cdk"})
        # Infrastructure-destroy gated tools register under
        # GCO_ENABLE_INFRASTRUCTURE_DESTROY.
        if "destroy_stack" in names:
            expected.update({"destroy_stack", "destroy_all"})
        # Local-file metric reader registers under GCO_ENABLE_LOCAL_METRICS.
        if "metrics_from_local_file" in names:
            expected.add("metrics_from_local_file")
        # Semantic-progress judge registers under GCO_ENABLE_SEMANTIC_PROGRESS.
        if "metrics_semantic_progress" in names:
            expected.add("metrics_semantic_progress")
        # The nine mission_* tools register together under GCO_ENABLE_MISSION.
        if "mission_start" in names:
            expected.update(
                {
                    "mission_start",
                    "mission_status",
                    "mission_iterate",
                    "mission_checkpoint",
                    "mission_complete",
                    "mission_abort",
                    "mission_resume",
                    "mission_history",
                    "mission_list",
                }
            )
        assert names == expected

    def test_each_tool_has_description(self):
        tools = asyncio.run(run_mcp.mcp._list_tools())
        for tool in tools:
            assert tool.description, f"Tool {tool.name} has no description"


class TestJobTools:
    """Tests for job management tools."""

    def test_list_jobs_all_regions(self):
        with patch("cli_runner.subprocess.run") as mock:
            mock.return_value = MagicMock(returncode=0, stdout='{"jobs":[]}', stderr="")
            run_mcp.list_jobs()
            cmd = mock.call_args[0][0]
            assert "--all-regions" in cmd

    def test_list_jobs_specific_region(self):
        with patch("cli_runner.subprocess.run") as mock:
            mock.return_value = MagicMock(returncode=0, stdout='{"jobs":[]}', stderr="")
            run_mcp.list_jobs(region="us-east-1")
            cmd = mock.call_args[0][0]
            assert "-r" in cmd
            assert "us-east-1" in cmd
            assert "--all-regions" not in cmd

    def test_list_jobs_with_filters(self):
        with patch("cli_runner.subprocess.run") as mock:
            mock.return_value = MagicMock(returncode=0, stdout="{}", stderr="")
            run_mcp.list_jobs(namespace="ml-jobs", status="running")
            cmd = mock.call_args[0][0]
            assert "-n" in cmd
            assert "ml-jobs" in cmd
            assert "-s" in cmd
            assert "running" in cmd

    def test_submit_job_sqs(self):
        with patch("cli_runner.subprocess.run") as mock:
            mock.return_value = MagicMock(returncode=0, stdout='{"status":"queued"}', stderr="")
            run_mcp.submit_job_sqs("job.yaml", "us-east-1")
            cmd = mock.call_args[0][0]
            assert "submit-sqs" in cmd
            assert "job.yaml" in cmd
            assert "us-east-1" in cmd

    def test_submit_job_sqs_with_options(self):
        with patch("cli_runner.subprocess.run") as mock:
            mock.return_value = MagicMock(returncode=0, stdout="{}", stderr="")
            run_mcp.submit_job_sqs("job.yaml", "us-west-2", namespace="ml", priority=50)
            cmd = mock.call_args[0][0]
            assert "-n" in cmd
            assert "ml" in cmd
            assert "--priority" in cmd
            assert "50" in cmd

    def test_submit_job_api(self):
        with patch("cli_runner.subprocess.run") as mock:
            mock.return_value = MagicMock(returncode=0, stdout="{}", stderr="")
            run_mcp.submit_job_api("job.yaml", namespace="test")
            cmd = mock.call_args[0][0]
            assert "submit" in cmd
            assert "-n" in cmd
            assert "test" in cmd

    def test_get_job(self):
        with patch("cli_runner.subprocess.run") as mock:
            mock.return_value = MagicMock(returncode=0, stdout="{}", stderr="")
            run_mcp.get_job("my-job", "us-east-1")
            cmd = mock.call_args[0][0]
            assert "get" in cmd
            assert "my-job" in cmd

    def test_get_job_logs(self):
        with patch("cli_runner.subprocess.run") as mock:
            mock.return_value = MagicMock(returncode=0, stdout="log output", stderr="")
            run_mcp.get_job_logs("my-job", "us-east-1", tail=500)
            cmd = mock.call_args[0][0]
            assert "logs" in cmd
            assert "500" in cmd

    @pytest.mark.asyncio
    @patch.dict(os.environ, {"GCO_ENABLE_DESTRUCTIVE_OPERATIONS": "true"})
    async def test_delete_job(self):
        # ``delete_job`` is gated by GCO_ENABLE_DESTRUCTIVE_OPERATIONS — reload
        # so the async wrapper is registered and rebound on ``run_mcp``.
        import importlib

        importlib.reload(run_mcp)
        with patch("cli_runner.subprocess.run") as mock:
            mock.return_value = MagicMock(returncode=0, stdout="{}", stderr="")
            await run_mcp.delete_job("old-job", "us-east-1")
            cmd = mock.call_args[0][0]
            assert "delete" in cmd
            assert "-y" in cmd

    def test_get_job_events(self):
        with patch("cli_runner.subprocess.run") as mock:
            mock.return_value = MagicMock(returncode=0, stdout="{}", stderr="")
            run_mcp.get_job_events("my-job", "us-east-1")
            cmd = mock.call_args[0][0]
            assert "events" in cmd

    def test_cluster_health_all(self):
        with patch("cli_runner.subprocess.run") as mock:
            mock.return_value = MagicMock(returncode=0, stdout="{}", stderr="")
            run_mcp.cluster_health()
            cmd = mock.call_args[0][0]
            assert "--all-regions" in cmd

    def test_cluster_health_region(self):
        with patch("cli_runner.subprocess.run") as mock:
            mock.return_value = MagicMock(returncode=0, stdout="{}", stderr="")
            run_mcp.cluster_health(region="us-east-1")
            cmd = mock.call_args[0][0]
            assert "-r" in cmd
            assert "--all-regions" not in cmd

    def test_queue_status(self):
        with patch("cli_runner.subprocess.run") as mock:
            mock.return_value = MagicMock(returncode=0, stdout="{}", stderr="")
            run_mcp.queue_status()
            cmd = mock.call_args[0][0]
            assert "queue-status" in cmd


class TestCapacityTools:
    """Tests for capacity tools."""

    def test_check_capacity(self):
        with patch("cli_runner.subprocess.run") as mock:
            mock.return_value = MagicMock(returncode=0, stdout="{}", stderr="")
            run_mcp.check_capacity("g4dn.xlarge", "us-east-1")
            cmd = mock.call_args[0][0]
            assert "g4dn.xlarge" in cmd
            assert "us-east-1" in cmd

    def test_capacity_status(self):
        with patch("cli_runner.subprocess.run") as mock:
            mock.return_value = MagicMock(returncode=0, stdout="{}", stderr="")
            run_mcp.capacity_status()
            cmd = mock.call_args[0][0]
            assert "status" in cmd

    def test_recommend_region_gpu(self):
        with patch("cli_runner.subprocess.run") as mock:
            mock.return_value = MagicMock(returncode=0, stdout="{}", stderr="")
            run_mcp.recommend_region(gpu=True)
            cmd = mock.call_args[0][0]
            assert "--gpu" in cmd

    def test_recommend_region_instance(self):
        with patch("cli_runner.subprocess.run") as mock:
            mock.return_value = MagicMock(returncode=0, stdout="{}", stderr="")
            run_mcp.recommend_region(instance_type="p4d.24xlarge")
            cmd = mock.call_args[0][0]
            assert "p4d.24xlarge" in cmd

    def test_spot_prices(self):
        with patch("cli_runner.subprocess.run") as mock:
            mock.return_value = MagicMock(returncode=0, stdout="{}", stderr="")
            run_mcp.spot_prices("g5.xlarge", "us-west-2")
            cmd = mock.call_args[0][0]
            assert "spot-prices" in cmd

    def test_ai_recommend_basic(self):
        with patch("cli_runner.subprocess.run") as mock:
            mock.return_value = MagicMock(returncode=0, stdout="{}", stderr="")
            run_mcp.ai_recommend(workload="Fine-tuning a 20B parameter LLM")
            cmd = mock.call_args[0][0]
            assert "ai-recommend" in cmd
            assert "-w" in cmd
            assert "Fine-tuning a 20B parameter LLM" in cmd

    def test_ai_recommend_with_options(self):
        with patch("cli_runner.subprocess.run") as mock:
            mock.return_value = MagicMock(returncode=0, stdout="{}", stderr="")
            run_mcp.ai_recommend(
                workload="Training job",
                instance_type="p4d.24xlarge",
                region="us-east-1",
                gpu=True,
                min_gpus=8,
                min_memory_gb=320,
                fault_tolerance="high",
                max_cost=25.0,
            )
            cmd = mock.call_args[0][0]
            assert "ai-recommend" in cmd
            assert "-i" in cmd
            assert "p4d.24xlarge" in cmd
            assert "-r" in cmd
            assert "--gpu" in cmd
            assert "--min-gpus" in cmd
            assert "--min-memory-gb" in cmd
            assert "--fault-tolerance" in cmd
            assert "high" in cmd
            assert "--max-cost" in cmd

    def test_list_reservations(self):
        with patch("cli_runner.subprocess.run") as mock:
            mock.return_value = MagicMock(returncode=0, stdout="{}", stderr="")
            run_mcp.list_reservations(instance_type="p5.48xlarge", region="us-east-1")
            cmd = mock.call_args[0][0]
            assert "reservations" in cmd
            assert "p5.48xlarge" in cmd
            assert "-r" in cmd

    def test_list_reservations_no_filters(self):
        with patch("cli_runner.subprocess.run") as mock:
            mock.return_value = MagicMock(returncode=0, stdout="{}", stderr="")
            run_mcp.list_reservations()
            cmd = mock.call_args[0][0]
            assert "reservations" in cmd

    def test_reservation_check(self):
        with patch("cli_runner.subprocess.run") as mock:
            mock.return_value = MagicMock(returncode=0, stdout="{}", stderr="")
            run_mcp.reservation_check("p4d.24xlarge", regions=["us-east-1"], block_duration=48)
            cmd = mock.call_args[0][0]
            assert "reservation-check" in cmd
            assert "p4d.24xlarge" in cmd
            assert "-r" in cmd
            assert "--block-duration" in cmd
            assert "48" in cmd

    def test_reservation_check_no_blocks(self):
        with patch("cli_runner.subprocess.run") as mock:
            mock.return_value = MagicMock(returncode=0, stdout="{}", stderr="")
            run_mcp.reservation_check("g5.48xlarge", include_blocks=False)
            cmd = mock.call_args[0][0]
            assert "reservation-check" in cmd
            assert "--no-blocks" in cmd

    @patch.dict(os.environ, {"GCO_ENABLE_CAPACITY_PURCHASE": "true"})
    def test_reserve_capacity_dry_run(self):
        # Re-register the tool with env var enabled
        import importlib

        importlib.reload(run_mcp)
        with patch("cli_runner.subprocess.run") as mock:
            mock.return_value = MagicMock(returncode=0, stdout="{}", stderr="")
            run_mcp.reserve_capacity("cb-0123456789abcdef0", "us-east-1", dry_run=True)
            cmd = mock.call_args[0][0]
            assert "reserve" in cmd
            assert "cb-0123456789abcdef0" in cmd
            assert "-r" in cmd
            assert "--dry-run" in cmd

    @patch.dict(os.environ, {"GCO_ENABLE_CAPACITY_PURCHASE": "true"})
    def test_reserve_capacity_no_dry_run(self):
        import importlib

        importlib.reload(run_mcp)
        with patch("cli_runner.subprocess.run") as mock:
            mock.return_value = MagicMock(returncode=0, stdout="{}", stderr="")
            run_mcp.reserve_capacity("cb-0123456789abcdef0", "us-east-1")
            cmd = mock.call_args[0][0]
            assert "reserve" in cmd
            assert "--dry-run" not in cmd


class TestInferenceTools:
    """Tests for inference endpoint tools."""

    def test_deploy_inference_basic(self):
        with patch("cli_runner.subprocess.run") as mock:
            mock.return_value = MagicMock(returncode=0, stdout="{}", stderr="")
            run_mcp.deploy_inference("my-llm", "vllm/vllm-openai:v0.17.0")
            cmd = mock.call_args[0][0]
            assert "deploy" in cmd
            assert "my-llm" in cmd
            assert "vllm/vllm-openai:v0.17.0" in cmd

    def test_deploy_inference_with_options(self):
        with patch("cli_runner.subprocess.run") as mock:
            mock.return_value = MagicMock(returncode=0, stdout="{}", stderr="")
            run_mcp.deploy_inference(
                "my-llm",
                "img:v1",
                gpu_count=4,
                replicas=2,
                region="us-east-1",
                env_vars=["MODEL=llama3"],
            )
            cmd = mock.call_args[0][0]
            assert "4" in cmd
            assert "2" in cmd
            assert "-r" in cmd
            assert "-e" in cmd
            assert "MODEL=llama3" in cmd

    def test_list_inference_endpoints(self):
        with patch("cli_runner.subprocess.run") as mock:
            mock.return_value = MagicMock(returncode=0, stdout="{}", stderr="")
            run_mcp.list_inference_endpoints(state="running")
            cmd = mock.call_args[0][0]
            assert "--state" in cmd
            assert "running" in cmd

    def test_inference_status(self):
        with patch("cli_runner.subprocess.run") as mock:
            mock.return_value = MagicMock(returncode=0, stdout="{}", stderr="")
            run_mcp.inference_status("my-llm")
            cmd = mock.call_args[0][0]
            assert "status" in cmd
            assert "my-llm" in cmd

    def test_scale_inference(self):
        with patch("cli_runner.subprocess.run") as mock:
            mock.return_value = MagicMock(returncode=0, stdout="{}", stderr="")
            run_mcp.scale_inference("my-llm", 5)
            cmd = mock.call_args[0][0]
            assert "scale" in cmd
            assert "5" in cmd

    def test_update_inference_image(self):
        with patch("cli_runner.subprocess.run") as mock:
            mock.return_value = MagicMock(returncode=0, stdout="{}", stderr="")
            run_mcp.update_inference_image("my-llm", "img:v2")
            cmd = mock.call_args[0][0]
            assert "update-image" in cmd
            assert "img:v2" in cmd

    def test_stop_inference(self):
        with patch("cli_runner.subprocess.run") as mock:
            mock.return_value = MagicMock(returncode=0, stdout="{}", stderr="")
            run_mcp.stop_inference("my-llm")
            cmd = mock.call_args[0][0]
            assert "stop" in cmd
            assert "-y" in cmd

    def test_start_inference(self):
        with patch("cli_runner.subprocess.run") as mock:
            mock.return_value = MagicMock(returncode=0, stdout="{}", stderr="")
            run_mcp.start_inference("my-llm")
            cmd = mock.call_args[0][0]
            assert "start" in cmd

    @pytest.mark.asyncio
    @patch.dict(os.environ, {"GCO_ENABLE_DESTRUCTIVE_OPERATIONS": "true"})
    async def test_delete_inference(self):
        # ``delete_inference`` is gated by GCO_ENABLE_DESTRUCTIVE_OPERATIONS —
        # reload so the async wrapper is registered and rebound on ``run_mcp``.
        import importlib

        importlib.reload(run_mcp)
        with patch("cli_runner.subprocess.run") as mock:
            mock.return_value = MagicMock(returncode=0, stdout="{}", stderr="")
            await run_mcp.delete_inference("my-llm")
            cmd = mock.call_args[0][0]
            assert "delete" in cmd
            assert "-y" in cmd

    def test_canary_deploy(self):
        with patch("cli_runner.subprocess.run") as mock:
            mock.return_value = MagicMock(returncode=0, stdout="{}", stderr="")
            run_mcp.canary_deploy("my-llm", "img:v2", weight=25)
            cmd = mock.call_args[0][0]
            assert "canary" in cmd
            assert "25" in cmd

    def test_promote_canary(self):
        with patch("cli_runner.subprocess.run") as mock:
            mock.return_value = MagicMock(returncode=0, stdout="{}", stderr="")
            run_mcp.promote_canary("my-llm")
            cmd = mock.call_args[0][0]
            assert "promote" in cmd
            assert "-y" in cmd

    def test_rollback_canary(self):
        with patch("cli_runner.subprocess.run") as mock:
            mock.return_value = MagicMock(returncode=0, stdout="{}", stderr="")
            run_mcp.rollback_canary("my-llm")
            cmd = mock.call_args[0][0]
            assert "rollback" in cmd
            assert "-y" in cmd

    def test_invoke_inference_basic(self):
        with patch("cli_runner.subprocess.run") as mock:
            mock.return_value = MagicMock(returncode=0, stdout='{"choices":[]}', stderr="")
            run_mcp.invoke_inference("my-llm", "Hello world")
            cmd = mock.call_args[0][0]
            assert "invoke" in cmd
            assert "my-llm" in cmd
            assert "-p" in cmd
            assert "Hello world" in cmd

    def test_invoke_inference_with_options(self):
        with patch("cli_runner.subprocess.run") as mock:
            mock.return_value = MagicMock(returncode=0, stdout="{}", stderr="")
            run_mcp.invoke_inference(
                "my-llm", "test", max_tokens=200, api_path="/v1/completions", region="us-east-1"
            )
            cmd = mock.call_args[0][0]
            assert "200" in cmd
            assert "--path" in cmd
            assert "/v1/completions" in cmd
            assert "-r" in cmd
            assert "us-east-1" in cmd

    def test_invoke_inference_with_stream(self):
        with patch("cli_runner.subprocess.run") as mock:
            mock.return_value = MagicMock(returncode=0, stdout="{}", stderr="")
            run_mcp.invoke_inference("my-llm", "test", stream=True)
            cmd = mock.call_args[0][0]
            assert "--stream" in cmd

    def test_chat_inference_basic(self):
        with patch("cli_runner.subprocess.run") as mock:
            mock.return_value = MagicMock(returncode=0, stdout="{}", stderr="")
            run_mcp.chat_inference("my-llm", [{"role": "user", "content": "Hi"}])
            cmd = mock.call_args[0][0]
            assert "invoke" in cmd
            assert "my-llm" in cmd
            assert "-d" in cmd
            assert "/v1/chat/completions" in cmd

    def test_chat_inference_with_options(self):
        with patch("cli_runner.subprocess.run") as mock:
            mock.return_value = MagicMock(returncode=0, stdout="{}", stderr="")
            run_mcp.chat_inference(
                "my-llm",
                [
                    {"role": "system", "content": "You are helpful"},
                    {"role": "user", "content": "Hi"},
                ],
                max_tokens=512,
                temperature=0.7,
                region="eu-west-1",
            )
            cmd = mock.call_args[0][0]
            assert "-r" in cmd
            assert "eu-west-1" in cmd
            # Verify temperature is in the JSON body
            data_arg_idx = cmd.index("-d") + 1
            import json

            body = json.loads(cmd[data_arg_idx])
            assert body["temperature"] == 0.7
            assert body["max_tokens"] == 512
            assert len(body["messages"]) == 2

    def test_chat_inference_with_stream(self):
        with patch("cli_runner.subprocess.run") as mock:
            mock.return_value = MagicMock(returncode=0, stdout="{}", stderr="")
            run_mcp.chat_inference(
                "my-llm",
                [{"role": "user", "content": "Hi"}],
                stream=True,
            )
            cmd = mock.call_args[0][0]
            assert "--stream" in cmd
            data_arg_idx = cmd.index("-d") + 1
            import json

            body = json.loads(cmd[data_arg_idx])
            assert body["stream"] is True

    def test_inference_health(self):
        with patch("cli_runner.subprocess.run") as mock:
            mock.return_value = MagicMock(returncode=0, stdout="{}", stderr="")
            run_mcp.inference_health("my-llm")
            cmd = mock.call_args[0][0]
            assert "health" in cmd
            assert "my-llm" in cmd

    def test_inference_health_with_region(self):
        with patch("cli_runner.subprocess.run") as mock:
            mock.return_value = MagicMock(returncode=0, stdout="{}", stderr="")
            run_mcp.inference_health("my-llm", region="us-east-1")
            cmd = mock.call_args[0][0]
            assert "-r" in cmd
            assert "us-east-1" in cmd

    def test_list_endpoint_models(self):
        with patch("cli_runner.subprocess.run") as mock:
            mock.return_value = MagicMock(returncode=0, stdout="{}", stderr="")
            run_mcp.list_endpoint_models("my-llm")
            cmd = mock.call_args[0][0]
            assert "models" in cmd
            assert "my-llm" in cmd

    def test_list_endpoint_models_with_region(self):
        with patch("cli_runner.subprocess.run") as mock:
            mock.return_value = MagicMock(returncode=0, stdout="{}", stderr="")
            run_mcp.list_endpoint_models("my-llm", region="eu-west-1")
            cmd = mock.call_args[0][0]
            assert "-r" in cmd
            assert "eu-west-1" in cmd


class TestCostTools:
    """Tests for cost tracking tools."""

    def test_cost_summary(self):
        with patch("cli_runner.subprocess.run") as mock:
            mock.return_value = MagicMock(returncode=0, stdout="{}", stderr="")
            run_mcp.cost_summary(days=7)
            cmd = mock.call_args[0][0]
            assert "summary" in cmd
            assert "7" in cmd

    def test_cost_by_region(self):
        with patch("cli_runner.subprocess.run") as mock:
            mock.return_value = MagicMock(returncode=0, stdout="{}", stderr="")
            run_mcp.cost_by_region()
            cmd = mock.call_args[0][0]
            assert "regions" in cmd

    def test_cost_trend(self):
        with patch("cli_runner.subprocess.run") as mock:
            mock.return_value = MagicMock(returncode=0, stdout="{}", stderr="")
            run_mcp.cost_trend(days=7)
            cmd = mock.call_args[0][0]
            assert "trend" in cmd
            assert "7" in cmd

    def test_cost_forecast(self):
        with patch("cli_runner.subprocess.run") as mock:
            mock.return_value = MagicMock(returncode=0, stdout="{}", stderr="")
            run_mcp.cost_forecast(days_ahead=60)
            cmd = mock.call_args[0][0]
            assert "forecast" in cmd
            assert "60" in cmd


class TestInfraTools:
    """Tests for infrastructure tools."""

    def test_list_stacks(self):
        with patch("cli_runner.subprocess.run") as mock:
            mock.return_value = MagicMock(returncode=0, stdout="stacks", stderr="")
            run_mcp.list_stacks()
            cmd = mock.call_args[0][0]
            assert "stacks" in cmd
            assert "list" in cmd

    def test_stack_status(self):
        with patch("cli_runner.subprocess.run") as mock:
            mock.return_value = MagicMock(returncode=0, stdout="{}", stderr="")
            run_mcp.stack_status("gco-us-east-1", "us-east-1")
            cmd = mock.call_args[0][0]
            assert "gco-us-east-1" in cmd
            assert "us-east-1" in cmd

    def test_fsx_status(self):
        with patch("cli_runner.subprocess.run") as mock:
            mock.return_value = MagicMock(returncode=0, stdout="{}", stderr="")
            run_mcp.fsx_status()
            cmd = mock.call_args[0][0]
            assert "fsx" in cmd
            assert "status" in cmd


class TestStorageTools:
    """Tests for storage tools."""

    def test_list_storage_contents(self):
        with patch("cli_runner.subprocess.run") as mock:
            mock.return_value = MagicMock(returncode=0, stdout="{}", stderr="")
            run_mcp.list_storage_contents("us-east-1")
            cmd = mock.call_args[0][0]
            assert "ls" in cmd
            assert "us-east-1" in cmd

    def test_list_storage_contents_with_path(self):
        with patch("cli_runner.subprocess.run") as mock:
            mock.return_value = MagicMock(returncode=0, stdout="{}", stderr="")
            run_mcp.list_storage_contents("us-east-1", path="/outputs")
            cmd = mock.call_args[0][0]
            assert "/outputs" in cmd

    def test_list_file_systems(self):
        with patch("cli_runner.subprocess.run") as mock:
            mock.return_value = MagicMock(returncode=0, stdout="{}", stderr="")
            run_mcp.list_file_systems()
            cmd = mock.call_args[0][0]
            assert "list" in cmd


class TestModelTools:
    """Tests for model weight tools."""

    def test_list_models(self):
        with patch("cli_runner.subprocess.run") as mock:
            mock.return_value = MagicMock(returncode=0, stdout="{}", stderr="")
            run_mcp.list_models()
            cmd = mock.call_args[0][0]
            assert "models" in cmd
            assert "list" in cmd

    def test_get_model_uri(self):
        with patch("cli_runner.subprocess.run") as mock:
            mock.return_value = MagicMock(returncode=0, stdout="s3://bucket/model", stderr="")
            result = run_mcp.get_model_uri("llama3-8b")
            cmd = mock.call_args[0][0]
            assert "uri" in cmd
            assert "llama3-8b" in cmd
            assert result == "s3://bucket/model"


class TestResourceRegistration:
    """Verify documentation and source code resources are registered."""

    def test_static_resource_count(self):
        resources = asyncio.run(run_mcp.mcp.list_resources())
        # docs://index, docs://README, docs://QUICKSTART, docs://CONTRIBUTING,
        # docs://examples/README, docs://examples/guide, docs://adr/index,
        # source://index,
        # k8s://manifests/index, iam://policies/index,
        # infra://index, infra://helm/charts.yaml,
        # demos://index, clients://index, scripts://index,
        # ci://index, tests://index,
        # config://index, config://cdk.json, config://feature-toggles, config://env-vars,
        # images://index, images://replication/status,
        # plus three always-on self-introspection resources:
        # mcp://gco/tools/index, mcp://gco/resources/index, mcp://gco/feature-flags.
        assert len(resources) == 26

    def test_static_resource_uris(self):
        resources = asyncio.run(run_mcp.mcp.list_resources())
        uris = {str(r.uri) for r in resources}
        assert "docs://gco/index" in uris
        assert "docs://gco/README" in uris
        assert "docs://gco/QUICKSTART" in uris
        assert "docs://gco/CONTRIBUTING" in uris
        assert "docs://gco/examples/README" in uris
        assert "docs://gco/examples/guide" in uris
        assert "docs://gco/adr/index" in uris
        assert "source://gco/index" in uris
        assert "k8s://gco/manifests/index" in uris
        assert "iam://gco/policies/index" in uris
        assert "infra://gco/index" in uris
        assert "demos://gco/index" in uris
        assert "clients://gco/index" in uris
        assert "scripts://gco/index" in uris
        assert "ci://gco/index" in uris
        assert "tests://gco/index" in uris
        assert "config://gco/index" in uris
        assert "config://gco/cdk.json" in uris
        assert "config://gco/feature-toggles" in uris
        assert "config://gco/env-vars" in uris
        assert "images://gco/index" in uris
        assert "images://gco/replication/status" in uris

    def test_resource_template_count(self):
        templates = asyncio.run(run_mcp.mcp.list_resource_templates())
        # docs/{doc_name}, docs/by-topic/{topic}, docs/by-related/{doc_name},
        # docs/packages/{package_name}, docs/adr/{adr_id},
        # examples/{example_name}, examples/by-category/{category},
        # examples/by-use-case/{use_case}, config/{filename}, file/{filepath},
        # k8s/manifests/{filename}, iam/policies/{filename}, infra/dockerfiles/{filename},
        # demos/{filename}, clients/{filename}, scripts/{filename},
        # ci/workflows, ci/actions, ci/scripts, ci/templates,
        # ci/codeql, ci/kind, ci/config,
        # tests/{filepath}, images/{name}/tags, images/{name}/{tag},
        # gco://jobs/{job_name}, gco://inference/{endpoint_name},
        # gco://k8s/{namespace}/{kind}/{name}, gco://cluster/{region}/topology,
        # costs://gco/summary/{days_window}, tasks://gco/{task_id},
        # mcp://gco/tools/{tool_name} (always-on self-introspection template).
        # mission://sessions/{session_id}, mission://sessions/{session_id}/report,
        # and mission://sessions/{session_id}/audit-replay register only when
        # GCO_ENABLE_MISSION (or the umbrella flag) is set; accept either count
        # to avoid coupling this assertion to whichever gating env the rest of
        # the suite happens to leave behind.
        assert len(templates) in (33, 36)

    def test_resource_template_uris(self):
        templates = asyncio.run(run_mcp.mcp.list_resource_templates())
        uris = {t.uri_template for t in templates}
        assert "docs://gco/docs/{doc_name}" in uris
        assert "docs://gco/packages/{package_name}" in uris
        assert "docs://gco/adr/{adr_id}" in uris
        assert "docs://gco/examples/{example_name}" in uris
        assert "source://gco/config/{filename}" in uris
        assert "source://gco/file/{filepath*}" in uris
        assert "demos://gco/{filename}" in uris
        assert "clients://gco/{filename}" in uris
        assert "scripts://gco/{filename}" in uris
        # ci:// resource templates — index lives at the static uri above.
        assert "ci://gco/workflows/{filename}" in uris
        assert "ci://gco/actions/{action_name}" in uris
        assert "ci://gco/scripts/{filename}" in uris
        assert "ci://gco/templates/{filename}" in uris
        assert "ci://gco/codeql/{filename}" in uris
        assert "ci://gco/kind/{filename}" in uris
        assert "ci://gco/config/{filename}" in uris
        # Image registry templates added by phase 10.
        assert "images://gco/{name}/tags" in uris
        assert "images://gco/{name}/{tag}" in uris
        # Live-state resource templates.
        assert "gco://jobs/{job_name}" in uris
        assert "gco://inference/{endpoint_name}" in uris
        assert "gco://k8s/{namespace}/{kind}/{name}" in uris
        assert "gco://cluster/{region}/topology" in uris
        assert "costs://gco/summary/{days_window}" in uris
        assert "tasks://gco/{task_id}" in uris


class TestDocResources:
    """Tests for documentation resources."""

    def test_docs_index_contains_sections(self):
        result = asyncio.run(run_mcp.mcp.read_resource("docs://gco/index"))
        content = result.contents[0].content
        assert "Resource Index" in content
        assert "docs://gco/docs/ARCHITECTURE" in content
        assert "docs://gco/examples/" in content

    def test_docs_index_contains_new_resource_groups(self):
        result = asyncio.run(run_mcp.mcp.read_resource("docs://gco/index"))
        content = result.contents[0].content
        assert "demos://gco/index" in content
        assert "clients://gco/index" in content
        assert "scripts://gco/index" in content

    def test_docs_index_categorizes_examples(self):
        result = asyncio.run(run_mcp.mcp.read_resource("docs://gco/index"))
        content = result.contents[0].content
        assert "### Jobs & Training" in content
        assert "### Inference" in content
        assert "### Schedulers" in content

    def test_docs_index_contains_quickstart_and_contributing(self):
        result = asyncio.run(run_mcp.mcp.read_resource("docs://gco/index"))
        content = result.contents[0].content
        assert "docs://gco/QUICKSTART" in content
        assert "docs://gco/CONTRIBUTING" in content
        assert "docs://gco/examples/README" in content

    def test_readme_resource_returns_content(self):
        result = asyncio.run(run_mcp.mcp.read_resource("docs://gco/README"))
        content = result.contents[0].content
        assert "GCO" in content
        assert len(content) > 100

    def test_quickstart_resource_returns_content(self):
        result = asyncio.run(run_mcp.mcp.read_resource("docs://gco/QUICKSTART"))
        content = result.contents[0].content
        assert len(content) > 100

    def test_contributing_resource_returns_content(self):
        result = asyncio.run(run_mcp.mcp.read_resource("docs://gco/CONTRIBUTING"))
        content = result.contents[0].content
        assert len(content) > 100

    def test_examples_readme_returns_content(self):
        result = asyncio.run(run_mcp.mcp.read_resource("docs://gco/examples/README"))
        content = result.contents[0].content
        assert "Example" in content or "example" in content
        assert len(content) > 100

    def test_doc_resource_reads_existing_doc(self):
        result = asyncio.run(run_mcp.mcp.read_resource("docs://gco/docs/CLI"))
        content = result.contents[0].content
        assert len(content) > 100

    def test_doc_resource_missing_returns_available(self):
        result = asyncio.run(run_mcp.mcp.read_resource("docs://gco/docs/NONEXISTENT"))
        content = result.contents[0].content
        assert "not found" in content
        assert "Available" in content

    def test_example_resource_reads_existing(self):
        result = asyncio.run(run_mcp.mcp.read_resource("docs://gco/examples/simple-job"))
        content = result.contents[0].content
        assert len(content) > 10

    def test_example_resource_missing_returns_available(self):
        result = asyncio.run(run_mcp.mcp.read_resource("docs://gco/examples/nonexistent"))
        content = result.contents[0].content
        assert "not found" in content


class TestSourceResources:
    """Tests for source code resources."""

    def test_source_index_contains_packages(self):
        result = asyncio.run(run_mcp.mcp.read_resource("source://gco/index"))
        content = result.contents[0].content
        assert "Source Code Index" in content
        assert "gco/" in content
        assert "cli/" in content

    def test_source_index_contains_new_packages(self):
        result = asyncio.run(run_mcp.mcp.read_resource("source://gco/index"))
        content = result.contents[0].content
        assert "scripts/" in content
        assert "demo/" in content
        assert "dockerfiles/" in content

    def test_source_index_lists_config_files(self):
        result = asyncio.run(run_mcp.mcp.read_resource("source://gco/index"))
        content = result.contents[0].content
        assert "source://gco/config/pyproject.toml" in content
        assert "source://gco/config/.gitlab-ci.yml" in content
        assert "source://gco/config/.pre-commit-config.yaml" in content

    def test_source_index_lists_files(self):
        result = asyncio.run(run_mcp.mcp.read_resource("source://gco/index"))
        content = result.contents[0].content
        assert "source://gco/file/" in content

    def test_config_resource_reads_pyproject(self):
        result = asyncio.run(run_mcp.mcp.read_resource("source://gco/config/pyproject.toml"))
        content = result.contents[0].content
        assert "gco-cli" in content

    def test_config_resource_reads_gitlab_ci(self):
        result = asyncio.run(run_mcp.mcp.read_resource("source://gco/config/.gitlab-ci.yml"))
        content = result.contents[0].content
        assert len(content) > 10

    def test_config_resource_reads_pre_commit(self):
        result = asyncio.run(
            run_mcp.mcp.read_resource("source://gco/config/.pre-commit-config.yaml")
        )
        content = result.contents[0].content
        assert len(content) > 10

    def test_config_resource_reads_checkov(self):
        result = asyncio.run(run_mcp.mcp.read_resource("source://gco/config/.checkov.yaml"))
        content = result.contents[0].content
        assert len(content) > 5

    def test_config_resource_rejects_unknown(self):
        result = asyncio.run(run_mcp.mcp.read_resource("source://gco/config/secret.env"))
        content = result.contents[0].content
        assert "Not available" in content

    def test_file_resource_reads_source(self):
        result = asyncio.run(run_mcp.mcp.read_resource("source://gco/file/cli/main.py"))
        content = result.contents[0].content
        assert "import" in content
        assert len(content) > 50

    def test_file_resource_reads_shell_script(self):
        result = asyncio.run(
            run_mcp.mcp.read_resource("source://gco/file/scripts/setup-cluster-access.sh")
        )
        content = result.contents[0].content
        assert len(content) > 50

    def test_file_resource_reads_markdown(self):
        result = asyncio.run(run_mcp.mcp.read_resource("source://gco/file/demo/README.md"))
        content = result.contents[0].content
        assert len(content) > 50

    def test_file_resource_missing_file(self):
        result = asyncio.run(run_mcp.mcp.read_resource("source://gco/file/cli/nonexistent.py"))
        content = result.contents[0].content
        assert "not found" in content

    def test_file_resource_rejects_bad_extension(self):
        result = asyncio.run(run_mcp.mcp.read_resource("source://gco/file/images/README.png"))
        content = result.contents[0].content
        assert "not served" in content or "not found" in content

    def test_file_resource_blocks_skipped_dirs(self):
        result = asyncio.run(
            run_mcp.mcp.read_resource("source://gco/file/cli/__pycache__/anything")
        )
        content = result.contents[0].content
        assert "denied" in content.lower()

    def test_file_resource_blocks_path_traversal(self):
        """Path traversal is blocked: FastMCP normalises URI segments so ../../
        collapses before reaching the handler, and the handler's resolve() +
        startswith check catches anything that still escapes."""
        import pytest
        from fastmcp.exceptions import NotFoundError

        # FastMCP normalises the URI, collapsing ../.. so the path no longer
        # matches the file/{filepath*} template — raises NotFoundError.
        with pytest.raises(NotFoundError):
            asyncio.run(run_mcp.mcp.read_resource("source://gco/file/../../etc/passwd"))


class TestDemoResources:
    """Tests for demo and walkthrough resources."""

    def test_demos_index_contains_walkthroughs(self):
        result = asyncio.run(run_mcp.mcp.read_resource("demos://gco/index"))
        content = result.contents[0].content
        assert "Walkthrough" in content
        assert "demos://gco/DEMO_WALKTHROUGH" in content
        assert "demos://gco/INFERENCE_WALKTHROUGH" in content
        assert "demos://gco/LIVE_DEMO" in content

    def test_demos_index_contains_scripts(self):
        result = asyncio.run(run_mcp.mcp.read_resource("demos://gco/index"))
        content = result.contents[0].content
        assert "demos://gco/live_demo.sh" in content
        assert "demos://gco/lib_demo.sh" in content

    def test_demo_reads_walkthrough_with_md_fallback(self):
        result = asyncio.run(run_mcp.mcp.read_resource("demos://gco/DEMO_WALKTHROUGH"))
        content = result.contents[0].content
        assert len(content) > 100

    def test_demo_reads_inference_walkthrough(self):
        result = asyncio.run(run_mcp.mcp.read_resource("demos://gco/INFERENCE_WALKTHROUGH"))
        content = result.contents[0].content
        assert len(content) > 100

    def test_demo_reads_live_demo_docs(self):
        result = asyncio.run(run_mcp.mcp.read_resource("demos://gco/LIVE_DEMO"))
        content = result.contents[0].content
        assert len(content) > 100

    def test_demo_reads_shell_script(self):
        result = asyncio.run(run_mcp.mcp.read_resource("demos://gco/live_demo.sh"))
        content = result.contents[0].content
        assert len(content) > 50

    def test_demo_reads_lib_script(self):
        result = asyncio.run(run_mcp.mcp.read_resource("demos://gco/lib_demo.sh"))
        content = result.contents[0].content
        assert len(content) > 50

    def test_demo_reads_readme(self):
        result = asyncio.run(run_mcp.mcp.read_resource("demos://gco/README.md"))
        content = result.contents[0].content
        assert "Demo" in content

    def test_demo_missing_returns_available(self):
        result = asyncio.run(run_mcp.mcp.read_resource("demos://gco/nonexistent.sh"))
        content = result.contents[0].content
        assert "not found" in content
        assert "Available" in content


class TestClientResources:
    """Tests for API client example resources."""

    def test_clients_index_contains_examples(self):
        result = asyncio.run(run_mcp.mcp.read_resource("clients://gco/index"))
        content = result.contents[0].content
        assert "Client Examples" in content
        assert "clients://gco/README" in content
        assert "python_boto3_example.py" in content
        assert "aws_cli_examples.sh" in content
        assert "curl_sigv4_proxy_example.sh" in content

    def test_client_reads_python_example(self):
        result = asyncio.run(run_mcp.mcp.read_resource("clients://gco/python_boto3_example.py"))
        content = result.contents[0].content
        assert len(content) > 50

    def test_client_reads_shell_example(self):
        result = asyncio.run(run_mcp.mcp.read_resource("clients://gco/aws_cli_examples.sh"))
        content = result.contents[0].content
        assert len(content) > 50

    def test_client_reads_curl_example(self):
        result = asyncio.run(run_mcp.mcp.read_resource("clients://gco/curl_sigv4_proxy_example.sh"))
        content = result.contents[0].content
        assert len(content) > 50

    def test_client_reads_readme_with_fallback(self):
        result = asyncio.run(run_mcp.mcp.read_resource("clients://gco/README"))
        content = result.contents[0].content
        assert "Client" in content or "API" in content
        assert len(content) > 100

    def test_client_missing_returns_available(self):
        result = asyncio.run(run_mcp.mcp.read_resource("clients://gco/nonexistent.py"))
        content = result.contents[0].content
        assert "not found" in content
        assert "Available" in content


class TestScriptResources:
    """Tests for utility script resources."""

    def test_scripts_index_contains_scripts(self):
        result = asyncio.run(run_mcp.mcp.read_resource("scripts://gco/index"))
        content = result.contents[0].content
        assert "Utility Scripts" in content
        assert "scripts://gco/README" in content
        assert "setup-cluster-access.sh" in content
        assert "bump_version.py" in content

    def test_script_reads_shell_script(self):
        result = asyncio.run(run_mcp.mcp.read_resource("scripts://gco/setup-cluster-access.sh"))
        content = result.contents[0].content
        assert len(content) > 50

    def test_script_reads_python_script(self):
        result = asyncio.run(run_mcp.mcp.read_resource("scripts://gco/bump_version.py"))
        content = result.contents[0].content
        assert len(content) > 50

    def test_script_reads_readme_with_fallback(self):
        result = asyncio.run(run_mcp.mcp.read_resource("scripts://gco/README"))
        content = result.contents[0].content
        assert "Scripts" in content or "scripts" in content
        assert len(content) > 50

    def test_script_missing_returns_available(self):
        result = asyncio.run(run_mcp.mcp.read_resource("scripts://gco/nonexistent.sh"))
        content = result.contents[0].content
        assert "not found" in content
        assert "Available" in content


class TestInfraResources:
    """Tests for infrastructure resources."""

    def test_infra_index_contains_security_section(self):
        result = asyncio.run(run_mcp.mcp.read_resource("infra://gco/index"))
        content = result.contents[0].content
        assert "Security" in content or "Linting" in content
        assert ".checkov.yaml" in content
        assert ".gitleaks.toml" in content

    def test_infra_index_contains_dockerfile_readme(self):
        result = asyncio.run(run_mcp.mcp.read_resource("infra://gco/index"))
        content = result.contents[0].content
        assert "infra://gco/dockerfiles/README.md" in content

    def test_infra_index_references_related_resources(self):
        result = asyncio.run(run_mcp.mcp.read_resource("infra://gco/index"))
        content = result.contents[0].content
        assert "scripts://gco/index" in content
        assert "demos://gco/index" in content

    def test_infra_dockerfile_readme(self):
        result = asyncio.run(run_mcp.mcp.read_resource("infra://gco/dockerfiles/README.md"))
        content = result.contents[0].content
        assert len(content) > 10

    def test_infra_dockerfile_unknown_filename_returns_available_list(self):
        """Reading ``infra://gco/dockerfiles/<missing>`` returns the available list.

        Pins the file-not-found branch in
        ``gco_mcp/resources/infra.py::dockerfile_resource``: the resource
        returns ``"File 'X' not found. Available:\\n..."`` rather than
        raising, so a tool-only client gets a guiding string body
        rather than an opaque transport error.
        """
        result = asyncio.run(
            run_mcp.mcp.read_resource("infra://gco/dockerfiles/nonexistent-zzz-file"),
        )
        content = result.contents[0].content
        assert "not found" in content
        assert "Available:" in content

    def test_infra_helm_charts_resource_returns_real_yaml(self):
        """``infra://gco/helm/charts.yaml`` reads the on-disk file verbatim.

        Exercises the happy path of ``helm_charts_resource``. The
        not-found branch (line 65) is untestable in this configuration
        because the file ships with the repo, but the read path needed
        coverage and was missing it.
        """
        result = asyncio.run(run_mcp.mcp.read_resource("infra://gco/helm/charts.yaml"))
        content = result.contents[0].content
        # The charts.yaml is real YAML keyed by chart name; assert
        # loose shape rather than pinning specific chart names that
        # could legitimately drift.
        assert len(content) > 10
        # Some line in the file must be valid YAML key syntax.
        assert ":" in content


class TestCIResources:
    """Tests for the ci:// resource group (``.github/`` tree).

    Each test reads a real file under ``.github/`` so a broken resolver or
    a rename in the repo will fail the suite — we don't stub the filesystem.
    """

    def test_ci_index_lists_workflows_section(self):
        result = asyncio.run(run_mcp.mcp.read_resource("ci://gco/index"))
        content = result.contents[0].content
        assert "# GitHub Actions & CI Configuration" in content
        # Section header and at least one real workflow file must show up.
        assert "## Workflows" in content
        assert "ci://gco/workflows/unit-tests.yml" in content

    def test_ci_index_lists_composite_actions(self):
        result = asyncio.run(run_mcp.mcp.read_resource("ci://gco/index"))
        content = result.contents[0].content
        assert "## Composite Actions" in content
        assert "ci://gco/actions/build-lambda-package" in content

    def test_ci_index_lists_scripts(self):
        result = asyncio.run(run_mcp.mcp.read_resource("ci://gco/index"))
        content = result.contents[0].content
        assert "## Scripts" in content
        assert "ci://gco/scripts/dependency-scan.sh" in content

    def test_ci_index_lists_templates(self):
        result = asyncio.run(run_mcp.mcp.read_resource("ci://gco/index"))
        content = result.contents[0].content
        assert "## Issue & PR Templates" in content
        assert "ci://gco/templates/bug_report.md" in content
        assert "ci://gco/templates/pull_request_template.md" in content

    def test_ci_index_lists_policy_and_automation(self):
        result = asyncio.run(run_mcp.mcp.read_resource("ci://gco/index"))
        content = result.contents[0].content
        assert "ci://gco/config/CI.md" in content
        assert "ci://gco/config/SECURITY.md" in content
        assert "ci://gco/config/CODEOWNERS" in content
        # dependabot / release-notes automation appear when the files exist.
        if (run_mcp.GITHUB_DIR / "release.yml").is_file():
            assert "ci://gco/config/release.yml" in content

    def test_ci_workflow_file_resolves(self):
        """A real workflow must be readable through the resource URI."""
        result = asyncio.run(run_mcp.mcp.read_resource("ci://gco/workflows/unit-tests.yml"))
        content = result.contents[0].content
        # Sanity: content starts with a GitHub Actions workflow header.
        assert "on:" in content or "on :" in content
        assert "jobs:" in content

    def test_ci_workflow_missing_file_lists_alternatives(self):
        result = asyncio.run(run_mcp.mcp.read_resource("ci://gco/workflows/does-not-exist.yml"))
        content = result.contents[0].content
        assert "not found" in content
        assert "Available" in content
        # The available list should include at least one real workflow.
        assert "unit-tests.yml" in content

    def test_ci_composite_action_resolves_without_trailing_path(self):
        """Action URIs take the directory name only — handler appends action.yml."""
        result = asyncio.run(run_mcp.mcp.read_resource("ci://gco/actions/build-lambda-package"))
        content = result.contents[0].content
        # action.yml starts with a `name:` key.
        assert content.lstrip().startswith("name:") or "\nname:" in content

    def test_ci_config_file_allowlist_blocks_unlisted_names(self):
        """The config/ resolver is strictly allowlisted so it can't be coerced
        into reading arbitrary files via path traversal or typos."""
        result = asyncio.run(run_mcp.mcp.read_resource("ci://gco/config/not-in-allowlist.yml"))
        content = result.contents[0].content
        assert "not in the served allowlist" in content

    def test_ci_config_readme_reads(self):
        result = asyncio.run(run_mcp.mcp.read_resource("ci://gco/config/CI.md"))
        content = result.contents[0].content
        assert "# " in content  # Markdown heading

    def test_docs_index_cross_references_ci_group(self):
        """Discoverability: the docs index should point users at ci://gco/index."""
        result = asyncio.run(run_mcp.mcp.read_resource("docs://gco/index"))
        content = result.contents[0].content
        assert "ci://gco/index" in content


# =============================================================================
# Argv-translation tests for the read-only async tools.
#
# These tools all dispatch through ``asyncio.to_thread(cli_runner._run_cli,
# *args)``, so mocking ``cli_runner.subprocess.run`` works the same as for
# the existing sync-tool tests — the call still goes through ``_run_cli``
# down to ``subprocess.run``. Each class covers a minimal-args path and a
# full/all-flags path so we lock both branches of the optional-flag logic.
# =============================================================================


class TestStacksReadOnlyTools:
    """Argv translation for the read-only stacks inspection tools."""

    @pytest.mark.asyncio
    async def test_stack_diff_no_args(self):
        with patch("cli_runner.subprocess.run") as mock:
            mock.return_value = MagicMock(returncode=0, stdout="{}", stderr="")
            await run_mcp.stack_diff()
            cmd = mock.call_args[0][0]
            assert os.path.basename(cmd[0]) == "gco"
            assert "stacks" in cmd
            assert "diff" in cmd
            # No stack name positional supplied.
            assert "gco-us-east-1" not in cmd

    @pytest.mark.asyncio
    async def test_stack_diff_with_stack_name(self):
        with patch("cli_runner.subprocess.run") as mock:
            mock.return_value = MagicMock(returncode=0, stdout="{}", stderr="")
            await run_mcp.stack_diff(stack_name="gco-us-east-1")
            cmd = mock.call_args[0][0]
            assert "stacks" in cmd
            assert "diff" in cmd
            assert "gco-us-east-1" in cmd

    @pytest.mark.asyncio
    async def test_stack_outputs_required_args(self):
        with patch("cli_runner.subprocess.run") as mock:
            mock.return_value = MagicMock(returncode=0, stdout="{}", stderr="")
            await run_mcp.stack_outputs(stack_name="gco-us-east-1", region="us-east-1")
            cmd = mock.call_args[0][0]
            assert "stacks" in cmd
            assert "outputs" in cmd
            assert "gco-us-east-1" in cmd
            assert cmd[cmd.index("-r") : cmd.index("-r") + 2] == ["-r", "us-east-1"]

    @pytest.mark.asyncio
    async def test_stack_synth_default_is_quiet(self):
        with patch("cli_runner.subprocess.run") as mock:
            mock.return_value = MagicMock(returncode=0, stdout="{}", stderr="")
            await run_mcp.stack_synth()
            cmd = mock.call_args[0][0]
            assert "stacks" in cmd
            assert "synth" in cmd
            assert "--quiet" in cmd
            # No stack name positional should be appended on the bare default call.
            assert "gco-us-east-1" not in cmd

    @pytest.mark.asyncio
    async def test_stack_synth_explicit_no_quiet_with_name(self):
        with patch("cli_runner.subprocess.run") as mock:
            mock.return_value = MagicMock(returncode=0, stdout="{}", stderr="")
            await run_mcp.stack_synth(stack_name="gco-us-east-1", quiet=False)
            cmd = mock.call_args[0][0]
            assert "stacks" in cmd
            assert "synth" in cmd
            assert "gco-us-east-1" in cmd
            assert "--quiet" not in cmd

    @pytest.mark.asyncio
    async def test_valkey_status(self):
        with patch("cli_runner.subprocess.run") as mock:
            mock.return_value = MagicMock(returncode=0, stdout="{}", stderr="")
            await run_mcp.valkey_status()
            cmd = mock.call_args[0][0]
            assert "stacks" in cmd
            assert "valkey" in cmd
            assert "status" in cmd

    @pytest.mark.asyncio
    async def test_aurora_status(self):
        with patch("cli_runner.subprocess.run") as mock:
            mock.return_value = MagicMock(returncode=0, stdout="{}", stderr="")
            await run_mcp.aurora_status()
            cmd = mock.call_args[0][0]
            assert "stacks" in cmd
            assert "aurora" in cmd
            assert "status" in cmd


class TestStacksMutatingTools:
    """Argv translation for the cdk.json enable/disable toggles. Every tool
    must include ``-y`` since the underlying CLI commands prompt for
    confirmation and the MCP wrapper has to be non-interactive."""

    @pytest.mark.asyncio
    async def test_enable_fsx(self):
        with patch("cli_runner.subprocess.run") as mock:
            mock.return_value = MagicMock(returncode=0, stdout="{}", stderr="")
            await run_mcp.enable_fsx()
            cmd = mock.call_args[0][0]
            assert "stacks" in cmd
            assert "fsx" in cmd
            assert "enable" in cmd
            assert "-y" in cmd

    @pytest.mark.asyncio
    async def test_disable_fsx(self):
        with patch("cli_runner.subprocess.run") as mock:
            mock.return_value = MagicMock(returncode=0, stdout="{}", stderr="")
            await run_mcp.disable_fsx()
            cmd = mock.call_args[0][0]
            assert "stacks" in cmd
            assert "fsx" in cmd
            assert "disable" in cmd
            assert "-y" in cmd

    @pytest.mark.asyncio
    async def test_enable_valkey(self):
        with patch("cli_runner.subprocess.run") as mock:
            mock.return_value = MagicMock(returncode=0, stdout="{}", stderr="")
            await run_mcp.enable_valkey()
            cmd = mock.call_args[0][0]
            assert "stacks" in cmd
            assert "valkey" in cmd
            assert "enable" in cmd
            assert "-y" in cmd

    @pytest.mark.asyncio
    async def test_disable_valkey(self):
        with patch("cli_runner.subprocess.run") as mock:
            mock.return_value = MagicMock(returncode=0, stdout="{}", stderr="")
            await run_mcp.disable_valkey()
            cmd = mock.call_args[0][0]
            assert "stacks" in cmd
            assert "valkey" in cmd
            assert "disable" in cmd
            assert "-y" in cmd

    @pytest.mark.asyncio
    async def test_enable_aurora(self):
        with patch("cli_runner.subprocess.run") as mock:
            mock.return_value = MagicMock(returncode=0, stdout="{}", stderr="")
            await run_mcp.enable_aurora()
            cmd = mock.call_args[0][0]
            assert "stacks" in cmd
            assert "aurora" in cmd
            assert "enable" in cmd
            assert "-y" in cmd

    @pytest.mark.asyncio
    async def test_disable_aurora(self):
        with patch("cli_runner.subprocess.run") as mock:
            mock.return_value = MagicMock(returncode=0, stdout="{}", stderr="")
            await run_mcp.disable_aurora()
            cmd = mock.call_args[0][0]
            assert "stacks" in cmd
            assert "aurora" in cmd
            assert "disable" in cmd
            assert "-y" in cmd


class TestQueueTools:
    """Argv translation for the read-only queue tools."""

    @pytest.mark.asyncio
    async def test_queue_list_no_args(self):
        with patch("cli_runner.subprocess.run") as mock:
            mock.return_value = MagicMock(returncode=0, stdout="[]", stderr="")
            await run_mcp.queue_list()
            cmd = mock.call_args[0][0]
            assert os.path.basename(cmd[0]) == "gco"
            assert "queue" in cmd
            assert "list" in cmd
            # The default limit=50 is always appended.
            assert "--limit" in cmd
            assert "50" in cmd
            # No region/namespace/status flags when those args were not supplied.
            assert "-r" not in cmd
            assert "-n" not in cmd
            assert "--status" not in cmd

    @pytest.mark.asyncio
    async def test_queue_list_full_args(self):
        with patch("cli_runner.subprocess.run") as mock:
            mock.return_value = MagicMock(returncode=0, stdout="[]", stderr="")
            await run_mcp.queue_list(region="us-east-1", status="running", namespace="ns", limit=10)
            cmd = mock.call_args[0][0]
            assert "queue" in cmd
            assert "list" in cmd
            assert cmd[cmd.index("-r") : cmd.index("-r") + 2] == ["-r", "us-east-1"]
            assert cmd[cmd.index("--status") : cmd.index("--status") + 2] == ["--status", "running"]
            assert cmd[cmd.index("-n") : cmd.index("-n") + 2] == ["-n", "ns"]
            assert cmd[cmd.index("--limit") : cmd.index("--limit") + 2] == ["--limit", "10"]

    @pytest.mark.asyncio
    async def test_queue_get_minimal(self):
        with patch("cli_runner.subprocess.run") as mock:
            mock.return_value = MagicMock(returncode=0, stdout="{}", stderr="")
            await run_mcp.queue_get(job_id="abc")
            cmd = mock.call_args[0][0]
            assert "queue" in cmd
            assert "get" in cmd
            assert "abc" in cmd
            assert "-r" not in cmd

    @pytest.mark.asyncio
    async def test_queue_get_with_region(self):
        with patch("cli_runner.subprocess.run") as mock:
            mock.return_value = MagicMock(returncode=0, stdout="{}", stderr="")
            await run_mcp.queue_get(job_id="abc", region="us-east-1")
            cmd = mock.call_args[0][0]
            assert "queue" in cmd
            assert "get" in cmd
            assert "abc" in cmd
            assert cmd[cmd.index("-r") : cmd.index("-r") + 2] == ["-r", "us-east-1"]

    @pytest.mark.asyncio
    async def test_queue_stats_no_args(self):
        with patch("cli_runner.subprocess.run") as mock:
            mock.return_value = MagicMock(returncode=0, stdout="{}", stderr="")
            await run_mcp.queue_stats()
            cmd = mock.call_args[0][0]
            assert "queue" in cmd
            assert "stats" in cmd
            assert "-r" not in cmd

    @pytest.mark.asyncio
    async def test_queue_stats_with_region(self):
        with patch("cli_runner.subprocess.run") as mock:
            mock.return_value = MagicMock(returncode=0, stdout="{}", stderr="")
            await run_mcp.queue_stats(region="us-east-1")
            cmd = mock.call_args[0][0]
            assert "queue" in cmd
            assert "stats" in cmd
            assert cmd[cmd.index("-r") : cmd.index("-r") + 2] == ["-r", "us-east-1"]

    @pytest.mark.asyncio
    async def test_queue_submit_minimal(self):
        with patch("cli_runner.subprocess.run") as mock:
            mock.return_value = MagicMock(returncode=0, stdout="{}", stderr="")
            await run_mcp.queue_submit(manifest_path="job.yaml", region="us-east-1")
            cmd = mock.call_args[0][0]
            assert "queue" in cmd
            assert "submit" in cmd
            assert "job.yaml" in cmd
            assert cmd[cmd.index("-r") : cmd.index("-r") + 2] == ["-r", "us-east-1"]
            # No optional flags should leak in.
            assert "-n" not in cmd
            assert "--priority" not in cmd
            assert "--label" not in cmd

    @pytest.mark.asyncio
    async def test_queue_submit_full_args(self):
        with patch("cli_runner.subprocess.run") as mock:
            mock.return_value = MagicMock(returncode=0, stdout="{}", stderr="")
            await run_mcp.queue_submit(
                manifest_path="job.yaml",
                region="us-east-1",
                namespace="ns",
                priority=42,
                labels={"team": "ml", "project": "training"},
            )
            cmd = mock.call_args[0][0]
            assert "queue" in cmd
            assert "submit" in cmd
            assert "job.yaml" in cmd
            assert cmd[cmd.index("-r") : cmd.index("-r") + 2] == ["-r", "us-east-1"]
            assert cmd[cmd.index("-n") : cmd.index("-n") + 2] == ["-n", "ns"]
            assert cmd[cmd.index("--priority") : cmd.index("--priority") + 2] == [
                "--priority",
                "42",
            ]
            # Two label flags, one per dict entry, in insertion order.
            label_flag_count = sum(1 for arg in cmd if arg == "--label")
            assert label_flag_count == 2
            assert "team=ml" in cmd
            assert "project=training" in cmd


class TestTemplatesTools:
    """Argv translation for the read-only templates tools."""

    @pytest.mark.asyncio
    async def test_templates_list_no_args(self):
        with patch("cli_runner.subprocess.run") as mock:
            mock.return_value = MagicMock(returncode=0, stdout="[]", stderr="")
            await run_mcp.templates_list()
            cmd = mock.call_args[0][0]
            assert "templates" in cmd
            assert "list" in cmd
            assert "-r" not in cmd

    @pytest.mark.asyncio
    async def test_templates_list_with_region(self):
        with patch("cli_runner.subprocess.run") as mock:
            mock.return_value = MagicMock(returncode=0, stdout="[]", stderr="")
            await run_mcp.templates_list(region="us-east-1")
            cmd = mock.call_args[0][0]
            assert "templates" in cmd
            assert "list" in cmd
            assert cmd[cmd.index("-r") : cmd.index("-r") + 2] == ["-r", "us-east-1"]

    @pytest.mark.asyncio
    async def test_templates_get_minimal(self):
        with patch("cli_runner.subprocess.run") as mock:
            mock.return_value = MagicMock(returncode=0, stdout="{}", stderr="")
            await run_mcp.templates_get(name="foo")
            cmd = mock.call_args[0][0]
            assert "templates" in cmd
            assert "get" in cmd
            assert "foo" in cmd
            assert "-r" not in cmd

    @pytest.mark.asyncio
    async def test_templates_get_with_region(self):
        with patch("cli_runner.subprocess.run") as mock:
            mock.return_value = MagicMock(returncode=0, stdout="{}", stderr="")
            await run_mcp.templates_get(name="foo", region="us-east-1")
            cmd = mock.call_args[0][0]
            assert "templates" in cmd
            assert "get" in cmd
            assert "foo" in cmd
            assert cmd[cmd.index("-r") : cmd.index("-r") + 2] == ["-r", "us-east-1"]

    @pytest.mark.asyncio
    async def test_templates_create_minimal(self):
        with patch("cli_runner.subprocess.run") as mock:
            mock.return_value = MagicMock(returncode=0, stdout="{}", stderr="")
            await run_mcp.templates_create(name="t1", manifest_path="job.yaml")
            cmd = mock.call_args[0][0]
            assert "templates" in cmd
            assert "create" in cmd
            # Positional order: name then manifest path.
            assert cmd[cmd.index("create") + 1] == "t1"
            assert cmd[cmd.index("create") + 2] == "job.yaml"
            assert "-r" not in cmd
            assert "-d" not in cmd

    @pytest.mark.asyncio
    async def test_templates_create_with_overrides(self):
        with patch("cli_runner.subprocess.run") as mock:
            mock.return_value = MagicMock(returncode=0, stdout="{}", stderr="")
            await run_mcp.templates_create(
                name="t1",
                manifest_path="job.yaml",
                region="us-east-1",
                description="GPU training template",
            )
            cmd = mock.call_args[0][0]
            assert "templates" in cmd
            assert "create" in cmd
            assert "t1" in cmd
            assert "job.yaml" in cmd
            assert cmd[cmd.index("-r") : cmd.index("-r") + 2] == ["-r", "us-east-1"]
            assert cmd[cmd.index("-d") : cmd.index("-d") + 2] == ["-d", "GPU training template"]

    @pytest.mark.asyncio
    async def test_templates_run_minimal(self):
        with patch("cli_runner.subprocess.run") as mock:
            mock.return_value = MagicMock(returncode=0, stdout="{}", stderr="")
            await run_mcp.templates_run(template_name="t1", job_name="j1", region="us-east-1")
            cmd = mock.call_args[0][0]
            assert "templates" in cmd
            assert "run" in cmd
            assert "t1" in cmd
            assert cmd[cmd.index("--name") : cmd.index("--name") + 2] == ["--name", "j1"]
            assert cmd[cmd.index("-r") : cmd.index("-r") + 2] == ["-r", "us-east-1"]
            assert "--namespace" not in cmd
            assert "-p" not in cmd
            assert "--priority" not in cmd

    @pytest.mark.asyncio
    async def test_templates_run_with_overrides(self):
        with patch("cli_runner.subprocess.run") as mock:
            mock.return_value = MagicMock(returncode=0, stdout="{}", stderr="")
            await run_mcp.templates_run(
                template_name="t1",
                job_name="j1",
                region="us-east-1",
                namespace="ns2",
                params=["image=custom:v1"],
            )
            cmd = mock.call_args[0][0]
            assert "templates" in cmd
            assert "run" in cmd
            assert "t1" in cmd
            assert cmd[cmd.index("--name") : cmd.index("--name") + 2] == ["--name", "j1"]
            assert cmd[cmd.index("-r") : cmd.index("-r") + 2] == ["-r", "us-east-1"]
            assert cmd[cmd.index("--namespace") : cmd.index("--namespace") + 2] == [
                "--namespace",
                "ns2",
            ]
            assert cmd[cmd.index("-p") : cmd.index("-p") + 2] == ["-p", "image=custom:v1"]
            assert "--priority" not in cmd


class TestWebhooksTools:
    """Argv translation for the read-only webhooks tools."""

    @pytest.mark.asyncio
    async def test_webhooks_list_no_args(self):
        with patch("cli_runner.subprocess.run") as mock:
            mock.return_value = MagicMock(returncode=0, stdout="[]", stderr="")
            await run_mcp.webhooks_list()
            cmd = mock.call_args[0][0]
            assert "webhooks" in cmd
            assert "list" in cmd
            assert "-r" not in cmd

    @pytest.mark.asyncio
    async def test_webhooks_list_with_region(self):
        with patch("cli_runner.subprocess.run") as mock:
            mock.return_value = MagicMock(returncode=0, stdout="[]", stderr="")
            await run_mcp.webhooks_list(region="us-east-1")
            cmd = mock.call_args[0][0]
            assert "webhooks" in cmd
            assert "list" in cmd
            assert cmd[cmd.index("-r") : cmd.index("-r") + 2] == ["-r", "us-east-1"]

    @pytest.mark.asyncio
    async def test_webhooks_get_minimal(self):
        with patch("cli_runner.subprocess.run") as mock:
            mock.return_value = MagicMock(returncode=0, stdout="{}", stderr="")
            await run_mcp.webhooks_get(name="hook1")
            cmd = mock.call_args[0][0]
            assert "webhooks" in cmd
            assert "get" in cmd
            assert "hook1" in cmd
            assert "-r" not in cmd

    @pytest.mark.asyncio
    async def test_webhooks_get_with_region(self):
        with patch("cli_runner.subprocess.run") as mock:
            mock.return_value = MagicMock(returncode=0, stdout="{}", stderr="")
            await run_mcp.webhooks_get(name="hook1", region="us-east-1")
            cmd = mock.call_args[0][0]
            assert "webhooks" in cmd
            assert "get" in cmd
            assert "hook1" in cmd
            assert cmd[cmd.index("-r") : cmd.index("-r") + 2] == ["-r", "us-east-1"]

    @pytest.mark.asyncio
    async def test_webhooks_create_basic(self):
        with patch("cli_runner.subprocess.run") as mock:
            mock.return_value = MagicMock(returncode=0, stdout="{}", stderr="")
            await run_mcp.webhooks_create(
                url="https://example.com/hook",
                events=["job.completed"],
            )
            cmd = mock.call_args[0][0]
            assert "webhooks" in cmd
            assert "create" in cmd
            assert cmd[cmd.index("--url") : cmd.index("--url") + 2] == [
                "--url",
                "https://example.com/hook",
            ]
            # One --event flag per entry.
            assert cmd.count("--event") == 1
            assert "job.completed" in cmd
            assert "-r" not in cmd
            assert "--secret" not in cmd

    @pytest.mark.asyncio
    async def test_webhooks_create_multiple_events(self):
        with patch("cli_runner.subprocess.run") as mock:
            mock.return_value = MagicMock(returncode=0, stdout="{}", stderr="")
            await run_mcp.webhooks_create(
                url="https://example.com/hook",
                events=["job.started", "job.completed", "job.failed"],
                region="us-east-1",
                secret="my-secret",
            )
            cmd = mock.call_args[0][0]
            assert "webhooks" in cmd
            assert "create" in cmd
            # Three --event flags.
            assert cmd.count("--event") == 3
            assert "job.started" in cmd
            assert "job.completed" in cmd
            assert "job.failed" in cmd
            assert cmd[cmd.index("-r") : cmd.index("-r") + 2] == ["-r", "us-east-1"]
            assert cmd[cmd.index("--secret") : cmd.index("--secret") + 2] == [
                "--secret",
                "my-secret",
            ]


class TestDagTools:
    """Argv translation for the read-only DAG tools."""

    @pytest.mark.asyncio
    async def test_dag_validate(self):
        with patch("cli_runner.subprocess.run") as mock:
            mock.return_value = MagicMock(returncode=0, stdout="{}", stderr="")
            await run_mcp.dag_validate(manifest_path="dag.yaml")
            cmd = mock.call_args[0][0]
            assert "dag" in cmd
            assert "validate" in cmd
            assert "dag.yaml" in cmd

    @pytest.mark.asyncio
    async def test_dag_validate_with_full_path(self):
        with patch("cli_runner.subprocess.run") as mock:
            mock.return_value = MagicMock(returncode=0, stdout="{}", stderr="")
            await run_mcp.dag_validate(manifest_path="examples/dags/sample.yaml")
            cmd = mock.call_args[0][0]
            assert "dag" in cmd
            assert "validate" in cmd
            assert "examples/dags/sample.yaml" in cmd

    @pytest.mark.asyncio
    async def test_dag_run(self):
        with patch("cli_runner.subprocess.run") as mock:
            mock.return_value = MagicMock(returncode=0, stdout="{}", stderr="")
            await run_mcp.dag_run(manifest_path="dag.yaml", region="us-east-1")
            cmd = mock.call_args[0][0]
            assert "dag" in cmd
            assert "run" in cmd
            assert "dag.yaml" in cmd
            assert cmd[cmd.index("-r") : cmd.index("-r") + 2] == ["-r", "us-east-1"]
            # Default dry_run=False — flag should not be present.
            assert "--dry-run" not in cmd

    @pytest.mark.asyncio
    async def test_dag_run_dry_run(self):
        with patch("cli_runner.subprocess.run") as mock:
            mock.return_value = MagicMock(returncode=0, stdout="{}", stderr="")
            await run_mcp.dag_run(manifest_path="dag.yaml", region="us-east-1", dry_run=True)
            cmd = mock.call_args[0][0]
            assert "dag" in cmd
            assert "run" in cmd
            assert "dag.yaml" in cmd
            assert "--dry-run" in cmd


class TestNodepoolsTools:
    """Argv translation for the read-only Karpenter nodepool tools."""

    @pytest.mark.asyncio
    async def test_nodepools_list_no_args(self):
        with patch("cli_runner.subprocess.run") as mock:
            mock.return_value = MagicMock(returncode=0, stdout="[]", stderr="")
            await run_mcp.nodepools_list()
            cmd = mock.call_args[0][0]
            assert "nodepools" in cmd
            assert "list" in cmd
            assert "-r" not in cmd
            assert "--cluster" not in cmd

    @pytest.mark.asyncio
    async def test_nodepools_list_full_args(self):
        with patch("cli_runner.subprocess.run") as mock:
            mock.return_value = MagicMock(returncode=0, stdout="[]", stderr="")
            await run_mcp.nodepools_list(region="us-east-1", cluster="my-cluster")
            cmd = mock.call_args[0][0]
            assert "nodepools" in cmd
            assert "list" in cmd
            assert cmd[cmd.index("-r") : cmd.index("-r") + 2] == ["-r", "us-east-1"]
            assert cmd[cmd.index("--cluster") : cmd.index("--cluster") + 2] == [
                "--cluster",
                "my-cluster",
            ]

    @pytest.mark.asyncio
    async def test_nodepools_describe_minimal(self):
        with patch("cli_runner.subprocess.run") as mock:
            mock.return_value = MagicMock(returncode=0, stdout="{}", stderr="")
            await run_mcp.nodepools_describe(nodepool_name="np1", region="us-east-1")
            cmd = mock.call_args[0][0]
            assert "nodepools" in cmd
            assert "describe" in cmd
            assert "np1" in cmd
            assert cmd[cmd.index("-r") : cmd.index("-r") + 2] == ["-r", "us-east-1"]
            assert "--cluster" not in cmd

    @pytest.mark.asyncio
    async def test_nodepools_describe_with_cluster(self):
        with patch("cli_runner.subprocess.run") as mock:
            mock.return_value = MagicMock(returncode=0, stdout="{}", stderr="")
            await run_mcp.nodepools_describe(nodepool_name="np1", region="us-east-1", cluster="c1")
            cmd = mock.call_args[0][0]
            assert "nodepools" in cmd
            assert "describe" in cmd
            assert "np1" in cmd
            assert cmd[cmd.index("-r") : cmd.index("-r") + 2] == ["-r", "us-east-1"]
            assert cmd[cmd.index("--cluster") : cmd.index("--cluster") + 2] == ["--cluster", "c1"]

    @pytest.mark.asyncio
    async def test_nodepools_create_odcr_minimal(self):
        with patch("cli_runner.subprocess.run") as mock:
            mock.return_value = MagicMock(returncode=0, stdout="{}", stderr="")
            await run_mcp.nodepools_create_odcr(
                name="gpu-reserved",
                region="us-east-1",
                capacity_reservation_id="cr-0123456789abcdef0",
                instance_type=["p4d.24xlarge"],
            )
            cmd = mock.call_args[0][0]
            assert "nodepools" in cmd
            assert "create-odcr" in cmd
            assert cmd[cmd.index("-n") : cmd.index("-n") + 2] == ["-n", "gpu-reserved"]
            assert cmd[cmd.index("-r") : cmd.index("-r") + 2] == ["-r", "us-east-1"]
            assert cmd[cmd.index("-c") : cmd.index("-c") + 2] == ["-c", "cr-0123456789abcdef0"]
            assert cmd[cmd.index("-i") : cmd.index("-i") + 2] == ["-i", "p4d.24xlarge"]
            assert cmd[cmd.index("--max-nodes") : cmd.index("--max-nodes") + 2] == [
                "--max-nodes",
                "100",
            ]
            # Optional flags absent when not supplied.
            assert "--fallback-on-demand" not in cmd
            assert "--efa" not in cmd

    @pytest.mark.asyncio
    async def test_nodepools_create_odcr_with_options(self):
        with patch("cli_runner.subprocess.run") as mock:
            mock.return_value = MagicMock(returncode=0, stdout="{}", stderr="")
            await run_mcp.nodepools_create_odcr(
                name="gpu-reserved",
                region="us-east-1",
                capacity_reservation_id="cr-0123456789abcdef0",
                instance_type=["p4d.24xlarge", "p5.48xlarge"],
                max_nodes=50,
                fallback_on_demand=True,
                efa=True,
            )
            cmd = mock.call_args[0][0]
            assert "nodepools" in cmd
            assert "create-odcr" in cmd
            assert cmd[cmd.index("--max-nodes") : cmd.index("--max-nodes") + 2] == [
                "--max-nodes",
                "50",
            ]
            assert cmd.count("-i") == 2
            assert "p4d.24xlarge" in cmd
            assert "p5.48xlarge" in cmd
            assert "--fallback-on-demand" in cmd
            assert "--efa" in cmd


class TestAnalyticsTools:
    """Argv translation for the read-only analytics tools."""

    @pytest.mark.asyncio
    async def test_analytics_doctor(self):
        with patch("cli_runner.subprocess.run") as mock:
            mock.return_value = MagicMock(returncode=0, stdout="{}", stderr="")
            await run_mcp.analytics_doctor()
            cmd = mock.call_args[0][0]
            assert "analytics" in cmd
            assert "doctor" in cmd

    @pytest.mark.asyncio
    async def test_analytics_login_url(self):
        with patch("cli_runner.subprocess.run") as mock:
            mock.return_value = MagicMock(returncode=0, stdout="{}", stderr="")
            await run_mcp.analytics_login_url(username="alice")
            cmd = mock.call_args[0][0]
            assert "analytics" in cmd
            assert "studio" in cmd
            assert "login" in cmd
            assert cmd[cmd.index("--username") : cmd.index("--username") + 2] == [
                "--username",
                "alice",
            ]

    @pytest.mark.asyncio
    async def test_analytics_users_list(self):
        with patch("cli_runner.subprocess.run") as mock:
            mock.return_value = MagicMock(returncode=0, stdout="[]", stderr="")
            await run_mcp.analytics_users_list()
            cmd = mock.call_args[0][0]
            assert "analytics" in cmd
            assert "users" in cmd
            assert "list" in cmd

    @pytest.mark.asyncio
    async def test_enable_analytics(self):
        with patch("cli_runner.subprocess.run") as mock:
            mock.return_value = MagicMock(returncode=0, stdout="{}", stderr="")
            await run_mcp.enable_analytics()
            cmd = mock.call_args[0][0]
            assert "analytics" in cmd
            assert "enable" in cmd
            assert "-y" in cmd
            # It's the top-level `analytics enable`, not `stacks analytics enable`.
            assert "stacks" not in cmd

    @pytest.mark.asyncio
    async def test_disable_analytics(self):
        with patch("cli_runner.subprocess.run") as mock:
            mock.return_value = MagicMock(returncode=0, stdout="{}", stderr="")
            await run_mcp.disable_analytics()
            cmd = mock.call_args[0][0]
            assert "analytics" in cmd
            assert "disable" in cmd
            assert "-y" in cmd
            assert "stacks" not in cmd

    @pytest.mark.asyncio
    async def test_analytics_user_add(self):
        with patch("cli_runner.subprocess.run") as mock:
            mock.return_value = MagicMock(returncode=0, stdout="{}", stderr="")
            await run_mcp.analytics_user_add(username="alice", email="alice@example.com")
            cmd = mock.call_args[0][0]
            assert "analytics" in cmd
            assert "users" in cmd
            assert "add" in cmd
            assert "alice" in cmd
            assert cmd[cmd.index("--email") : cmd.index("--email") + 2] == [
                "--email",
                "alice@example.com",
            ]
            # This CLI surface is interactive-friendly already — no -y flag.
            assert "-y" not in cmd


class TestConfigTools:
    """Argv translation for the read-only CLI config tools."""

    @pytest.mark.asyncio
    async def test_config_get_no_key(self):
        with patch("cli_runner.subprocess.run") as mock:
            mock.return_value = MagicMock(returncode=0, stdout="{}", stderr="")
            await run_mcp.config_get()
            cmd = mock.call_args[0][0]
            assert "config-cmd" in cmd
            assert "get" in cmd
            # When no key is supplied, the positional should be absent.
            assert "some.key" not in cmd

    @pytest.mark.asyncio
    async def test_config_get_with_key(self):
        with patch("cli_runner.subprocess.run") as mock:
            mock.return_value = MagicMock(returncode=0, stdout='"value"', stderr="")
            await run_mcp.config_get(key="some.key")
            cmd = mock.call_args[0][0]
            assert "config-cmd" in cmd
            assert "get" in cmd
            assert "some.key" in cmd


class TestStorageReadOnlyTools:
    """Argv translation for the read-only storage tools."""

    @pytest.mark.asyncio
    async def test_files_get(self):
        with patch("cli_runner.subprocess.run") as mock:
            mock.return_value = MagicMock(returncode=0, stdout="contents", stderr="")
            await run_mcp.files_get(region="us-east-1")
            cmd = mock.call_args[0][0]
            assert "files" in cmd
            assert "get" in cmd
            # `files get REGION [-t type]` — region is positional, no -r.
            assert "us-east-1" in cmd
            assert "-r" not in cmd
            assert cmd[cmd.index("-t") : cmd.index("-t") + 2] == ["-t", "efs"]

    @pytest.mark.asyncio
    async def test_files_access_points_no_args(self):
        with patch("cli_runner.subprocess.run") as mock:
            mock.return_value = MagicMock(returncode=0, stdout="[]", stderr="")
            await run_mcp.files_access_points()
            cmd = mock.call_args[0][0]
            assert "files" in cmd
            assert "access-points" in cmd
            assert "-r" not in cmd

    @pytest.mark.asyncio
    async def test_files_access_points_with_region(self):
        with patch("cli_runner.subprocess.run") as mock:
            mock.return_value = MagicMock(returncode=0, stdout="[]", stderr="")
            await run_mcp.files_access_points(region="us-east-1")
            cmd = mock.call_args[0][0]
            assert "files" in cmd
            assert "access-points" in cmd
            assert cmd[cmd.index("-r") : cmd.index("-r") + 2] == ["-r", "us-east-1"]


# =============================================================================
# Audit-decorator coverage on a representative async tool: success + error.
# These guard that the audit_logged wrapper still emits structured entries
# for the new async tools, both on the happy path and when the underlying
# CLI boundary raises.
# =============================================================================


class TestQueueToolsAuditCoverage:
    """End-to-end audit log coverage on an async read-only tool."""

    @pytest.mark.asyncio
    async def test_queue_list_success_audit_entry(self, caplog):
        with (
            caplog.at_level(logging.INFO, logger="gco.mcp.audit"),
            patch("cli_runner.subprocess.run") as mock,
        ):
            mock.return_value = MagicMock(returncode=0, stdout="[]", stderr="")
            await run_mcp.queue_list()

        records = [r for r in caplog.records if r.name == "gco.mcp.audit"]
        invocations = [
            json.loads(r.message)
            for r in records
            if json.loads(r.message).get("event") == "mcp.tool.invocation"
        ]
        assert any(e["tool"] == "queue_list" and e["status"] == "success" for e in invocations)

    @pytest.mark.asyncio
    async def test_queue_list_error_audit_entry(self, caplog):
        with (
            caplog.at_level(logging.INFO, logger="gco.mcp.audit"),
            patch("cli_runner._run_cli", side_effect=RuntimeError("boom")),
            pytest.raises(RuntimeError),
        ):
            await run_mcp.queue_list()

        records = [r for r in caplog.records if r.name == "gco.mcp.audit"]
        invocations = [
            json.loads(r.message)
            for r in records
            if json.loads(r.message).get("event") == "mcp.tool.invocation"
        ]
        error_entries = [
            e for e in invocations if e["tool"] == "queue_list" and e["status"] == "error"
        ]
        assert error_entries
        assert "boom" in error_entries[-1]["error"]
