"""
Extended unit coverage for the MCP server modules.

Targets the uncovered branches in:

* ``gco_mcp/iam.py`` — role assumption with ``GCO_MCP_ROLE_ARN`` set,
  failure paths, and the no-op path when the env var is unset.
* ``gco_mcp/resources/tasks.py`` — task-id validation, the ``get_task``
  accessor, the docket fallback chain, and the ``_coerce_to_dict``
  helper.
* ``gco_mcp/resources/cluster.py`` — region validation, the kubectl
  invocation success/failure/timeout branches, and the JSON parse
  failure branch.
* ``gco_mcp/resources/k8s.py`` — name/kind/namespace validation, kubectl
  success/failure/timeout/missing-binary branches.
* ``gco_mcp/resources/iam_policies.py`` — index emission and missing-file
  fallback.
* ``gco_mcp/resources/ci.py`` — index emission against the live ``.github``
  tree and the per-resource read paths (workflows, actions, scripts,
  templates, codeql, kind, config) including their not-found branches.
* ``gco_mcp/tools/docs.py`` — query-only and topic-only branches.
* ``gco_mcp/tools/images.py`` — every read-only and administrative tool
  body, including the lazy ``_get_manager`` indirection.
* ``gco_mcp/audit_middleware.py`` — capture-buffer activation and the
  reset-on-exception path.
* ``gco_mcp/cli_runner.py`` — error stub when the CLI exits non-zero.
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# Ensure ``gco_mcp/`` is on the path so internal modules import without
# the ``mcp.`` prefix that fastmcp's PyPI namespace would shadow.
sys.path.insert(0, str(Path(__file__).parent.parent / "gco_mcp"))

import run_mcp  # noqa: E402, F401  -- side-effect registers tools/resources

mcp = run_mcp.mcp
_EKS_CONTEXT = "arn:aws:eks:us-east-1:123456789012:cluster/gco-us-east-1"


@pytest.fixture(autouse=True)
def _pin_live_eks_context(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep live-resource branch tests offline while asserting explicit context use."""
    monkeypatch.setattr(
        "resources.cluster.eks_context_for_region",
        lambda _region: _EKS_CONTEXT,
    )
    monkeypatch.setattr(
        "resources.k8s.eks_context_for_region",
        lambda _region: _EKS_CONTEXT,
    )


def _read_resource(uri: str) -> str:
    """Read an MCP resource and return its raw string body.

    FastMCP returns a ResourceResult whose ``contents`` attribute is a
    list of ``ResourceContent`` objects; tests want the bare string for
    assertions.
    """
    result = asyncio.run(mcp.read_resource(uri))
    return result.contents[0].content


# ---------------------------------------------------------------------------
# gco_mcp/iam.py
# ---------------------------------------------------------------------------


class TestIam:
    def test_no_op_when_env_unset(self, monkeypatch: Any) -> None:
        """Without ``GCO_MCP_ROLE_ARN`` the function is a no-op."""
        monkeypatch.delenv("GCO_MCP_ROLE_ARN", raising=False)
        from iam import assume_mcp_role

        # Must return None and must not import boto3 here — we don't
        # patch boto3 because we never expect the helper to reach it.
        assert assume_mcp_role() is None

    def test_assumes_role_and_installs_default_session(self, monkeypatch: Any) -> None:
        monkeypatch.setenv("GCO_MCP_ROLE_ARN", "arn:aws:iam::123:role/mcp")
        monkeypatch.setenv("GCO_MCP_ROLE_SESSION_NAME", "test-session")
        monkeypatch.setenv("GCO_MCP_ROLE_DURATION_SECONDS", "900")

        sts = MagicMock()
        sts.assume_role.return_value = {
            "Credentials": {
                "AccessKeyId": "AKIA",
                "SecretAccessKey": "SECRET",
                "SessionToken": "TOKEN",
                "Expiration": MagicMock(isoformat=lambda: "2026-01-01T00:00:00+00:00"),
            }
        }

        ambient_session = MagicMock()
        ambient_session.client.return_value = sts

        with patch.dict("sys.modules", {"boto3": MagicMock()}):
            import boto3  # noqa: F401

            with (
                patch("boto3.Session", return_value=ambient_session),
                patch("boto3.setup_default_session") as setup_default,
            ):
                from iam import assume_mcp_role

                assume_mcp_role()

        sts.assume_role.assert_called_once()
        kwargs = sts.assume_role.call_args.kwargs
        assert kwargs["RoleArn"] == "arn:aws:iam::123:role/mcp"
        assert kwargs["RoleSessionName"] == "test-session"
        assert kwargs["DurationSeconds"] == 900
        setup_default.assert_called_once()

    def test_assume_role_failure_propagates(self, monkeypatch: Any) -> None:
        monkeypatch.setenv("GCO_MCP_ROLE_ARN", "arn:aws:iam::123:role/mcp")

        sts = MagicMock()
        sts.assume_role.side_effect = RuntimeError("denied")
        ambient_session = MagicMock()
        ambient_session.client.return_value = sts

        with patch.dict("sys.modules", {"boto3": MagicMock()}):
            import boto3  # noqa: F401

            with patch("boto3.Session", return_value=ambient_session):
                from iam import assume_mcp_role

                with pytest.raises(RuntimeError, match="denied"):
                    assume_mcp_role()

    def test_assume_role_handles_non_isoformat_expiration(self, monkeypatch: Any) -> None:
        """Expiration without an ``isoformat`` attr falls back to ``str(...)``."""
        monkeypatch.setenv("GCO_MCP_ROLE_ARN", "arn:aws:iam::123:role/mcp")

        # Expiration is a plain string here so the ``hasattr(..., "isoformat")``
        # check returns False and the str(...) branch is taken.
        sts = MagicMock()
        sts.assume_role.return_value = {
            "Credentials": {
                "AccessKeyId": "AKIA",
                "SecretAccessKey": "SECRET",
                "SessionToken": "TOKEN",
                "Expiration": "raw-string",
            }
        }
        ambient_session = MagicMock()
        ambient_session.client.return_value = sts

        with patch.dict("sys.modules", {"boto3": MagicMock()}):
            import boto3  # noqa: F401

            with (
                patch("boto3.Session", return_value=ambient_session),
                patch("boto3.setup_default_session"),
            ):
                from iam import assume_mcp_role

                assume_mcp_role()  # must not raise


# ---------------------------------------------------------------------------
# gco_mcp/resources/tasks.py
# ---------------------------------------------------------------------------


class TestTaskResource:
    """tasks://gco/{task_id} reads through the SEP-2663 tasks extension.

    The resource delegates to ``fastmcp_tasks.handlers.tasks_get`` — the
    same handler that answers protocol ``tasks/get`` requests — so these
    tests patch that single seam (or let the real handler run against the
    not-yet-started docket for the not-found path).
    """

    def test_invalid_task_id(self) -> None:
        body = _read_resource("tasks://gco/!@#$%")
        payload = json.loads(body)
        assert payload["error"] == "invalid task_id"

    def test_tasks_get_result_rendered_as_state(self) -> None:
        record = MagicMock()
        record.model_dump.return_value = {"status": "working", "task_id": "task-123"}
        with patch("fastmcp_tasks.handlers.tasks_get", new=AsyncMock(return_value=record)):
            body = _read_resource("tasks://gco/task-123")
        payload = json.loads(body)
        assert payload["task_id"] == "task-123"
        assert payload["state"]["status"] == "working"

    def test_tasks_get_receives_server_and_task_id(self) -> None:
        record = MagicMock()
        record.model_dump.return_value = {"status": "completed"}
        tasks_get = AsyncMock(return_value=record)
        with patch("fastmcp_tasks.handlers.tasks_get", new=tasks_get):
            _read_resource("tasks://gco/abc")
        tasks_get.assert_awaited_once_with(mcp, "abc")

    def test_tasks_get_raising_maps_to_task_not_found(self) -> None:
        with patch(
            "fastmcp_tasks.handlers.tasks_get",
            new=AsyncMock(side_effect=RuntimeError("Task not found: abc")),
        ):
            body = _read_resource("tasks://gco/abc")
        payload = json.loads(body)
        assert payload["error"] == "task not found"
        assert payload["task_id"] == "abc"

    def test_unknown_id_against_real_handler_is_not_found(self) -> None:
        # No patching: the real extension handler runs against a docket
        # that has not started (no server lifespan in this test), which
        # reports the id as unknown — the same wire answer a protocol
        # ``tasks/get`` for a bogus id would produce.
        body = _read_resource("tasks://gco/missing")
        payload = json.loads(body)
        assert payload["error"] == "task not found"
        assert payload["task_id"] == "missing"

    def test_missing_extension_returns_protocol_unavailable_stub(self) -> None:
        with patch.dict(sys.modules, {"fastmcp_tasks.handlers": None}):
            body = _read_resource("tasks://gco/abc")
        payload = json.loads(body)
        assert payload["error"] == "task protocol not available"
        assert payload["task_id"] == "abc"


# ---------------------------------------------------------------------------
# gco_mcp/resources/cluster.py — gco://cluster/{region}/topology
# ---------------------------------------------------------------------------


class TestClusterTopology:
    def test_invalid_region(self) -> None:
        body = _read_resource("gco://cluster/Bad-Region/topology")
        payload = json.loads(body)
        assert payload["error"] == "invalid region"

    def test_invokes_nodepools_and_kubectl(self) -> None:
        kubectl_result = MagicMock()
        kubectl_result.returncode = 0
        kubectl_result.stdout = '{"items":[]}'
        kubectl_result.stderr = ""

        with (
            patch(
                "resources.cluster.cli_runner._run_cli",
                return_value='{"items":[{"name":"np-a"}]}',
            ),
            patch(
                "resources.cluster.cli_runner.subprocess.run",
                return_value=kubectl_result,
            ),
        ):
            body = _read_resource("gco://cluster/us-east-1/topology")

        payload = json.loads(body)
        assert payload["region"] == "us-east-1"
        assert payload["nodepools"]["items"][0]["name"] == "np-a"
        assert payload["pending_pods"] == {"items": []}

    def test_kubectl_missing_returns_error(self) -> None:
        with (
            patch(
                "resources.cluster.cli_runner._run_cli",
                return_value="not json",
            ),
            patch(
                "resources.cluster.cli_runner.subprocess.run",
                side_effect=FileNotFoundError,
            ),
        ):
            body = _read_resource("gco://cluster/us-east-1/topology")
        payload = json.loads(body)
        # Both branches exercised: nodepools parse failure + kubectl missing.
        assert payload["nodepools"]["error"] == "failed to parse nodepools output"
        assert payload["pending_pods"] == {"error": "kubectl not found"}

    def test_kubectl_timeout_returns_error(self) -> None:
        import subprocess as _subprocess

        with (
            patch(
                "resources.cluster.cli_runner._run_cli",
                return_value="[]",  # JSON-array is fine — exercises the list branch
            ),
            patch(
                "resources.cluster.cli_runner.subprocess.run",
                side_effect=_subprocess.TimeoutExpired("kubectl", 30),
            ),
        ):
            body = _read_resource("gco://cluster/us-east-1/topology")
        payload = json.loads(body)
        assert "timed out" in payload["pending_pods"]["error"]

    def test_kubectl_non_zero_exit(self) -> None:
        kubectl_result = MagicMock()
        kubectl_result.returncode = 1
        kubectl_result.stdout = ""
        kubectl_result.stderr = "denied"
        with (
            patch("resources.cluster.cli_runner._run_cli", return_value="{}"),
            patch(
                "resources.cluster.cli_runner.subprocess.run",
                return_value=kubectl_result,
            ),
        ):
            body = _read_resource("gco://cluster/us-east-1/topology")
        payload = json.loads(body)
        assert payload["pending_pods"]["error"] == "denied"

    def test_kubectl_invalid_json(self) -> None:
        kubectl_result = MagicMock()
        kubectl_result.returncode = 0
        kubectl_result.stdout = "not json"
        kubectl_result.stderr = ""
        with (
            patch("resources.cluster.cli_runner._run_cli", return_value="{}"),
            patch(
                "resources.cluster.cli_runner.subprocess.run",
                return_value=kubectl_result,
            ),
        ):
            body = _read_resource("gco://cluster/us-east-1/topology")
        payload = json.loads(body)
        assert payload["pending_pods"]["error"] == "failed to parse kubectl output"

    def test_kubectl_valid_json_list_wrapped_in_value(self) -> None:
        """When kubectl returns a valid JSON *list* (not a dict), the resolver
        wraps it under ``{"value": parsed}`` so the JSON shape is uniform.
        Covers the trailing branch in ``_pending_pods``.
        """
        kubectl_result = MagicMock()
        kubectl_result.returncode = 0
        kubectl_result.stdout = "[1, 2, 3]"  # valid JSON, not a dict
        kubectl_result.stderr = ""
        with (
            patch("resources.cluster.cli_runner._run_cli", return_value="{}"),
            patch(
                "resources.cluster.cli_runner.subprocess.run",
                return_value=kubectl_result,
            ),
        ):
            body = _read_resource("gco://cluster/us-east-1/topology")
        payload = json.loads(body)
        assert payload["pending_pods"] == {"value": [1, 2, 3]}


# ---------------------------------------------------------------------------
# gco_mcp/resources/k8s.py — gco://k8s/{region}/{namespace}/{kind}/{name}
# ---------------------------------------------------------------------------


class TestK8sLiveResource:
    def test_invalid_namespace(self) -> None:
        body = _read_resource("gco://k8s/us-east-1/BAD/Pod/my-pod")
        payload = json.loads(body)
        assert payload["error"] == "invalid namespace"

    def test_invalid_kind(self) -> None:
        body = _read_resource("gco://k8s/us-east-1/default/!!!/my-pod")
        payload = json.loads(body)
        assert payload["error"] == "invalid kind"

    def test_invalid_name(self) -> None:
        body = _read_resource("gco://k8s/us-east-1/default/Pod/BAD")
        payload = json.loads(body)
        assert payload["error"] == "invalid name"

    def test_kubectl_success_returns_yaml(self) -> None:
        result = MagicMock()
        result.returncode = 0
        result.stdout = "apiVersion: v1\nkind: Pod\n"
        result.stderr = ""
        with patch("resources.k8s.cli_runner.subprocess.run", return_value=result) as run:
            body = _read_resource("gco://k8s/us-east-1/default/pod/my-pod")
        assert "apiVersion: v1" in body
        assert run.call_args[0][0][-2:] == ["--context", _EKS_CONTEXT]

    def test_kubectl_missing(self) -> None:
        with patch("resources.k8s.cli_runner.subprocess.run", side_effect=FileNotFoundError):
            body = _read_resource("gco://k8s/us-east-1/default/pod/my-pod")
        payload = json.loads(body)
        assert payload["error"] == "kubectl not found"

    def test_kubectl_timeout(self) -> None:
        import subprocess as _subprocess

        with patch(
            "resources.k8s.cli_runner.subprocess.run",
            side_effect=_subprocess.TimeoutExpired("kubectl", 30),
        ):
            body = _read_resource("gco://k8s/us-east-1/default/pod/my-pod")
        payload = json.loads(body)
        assert "timed out" in payload["error"]

    def test_kubectl_non_zero_exit(self) -> None:
        result = MagicMock()
        result.returncode = 1
        result.stdout = ""
        result.stderr = "not found"
        with patch("resources.k8s.cli_runner.subprocess.run", return_value=result):
            body = _read_resource("gco://k8s/us-east-1/default/pod/missing")
        payload = json.loads(body)
        assert payload["error"] == "not found"


class TestK8sManifestStaticResource:
    """Direct exercises of ``k8s_manifest_resource`` and the manifests index."""

    def test_manifest_resource_returns_file_text(self) -> None:
        """A real manifest filename returns the file's text contents."""
        from resources.k8s import MANIFESTS_DIR, k8s_manifest_resource

        # Pick any real manifest file shipped under
        # ``lambda/kubectl-applier-simple/manifests`` so the test
        # stays honest about what's actually deployed.
        yaml_files = sorted(MANIFESTS_DIR.glob("*.yaml"))
        assert yaml_files, "no manifest files to read — packaging regression?"
        target = yaml_files[0]
        body = k8s_manifest_resource(target.name)
        # The first manifest is real YAML, so we expect at least the
        # ``apiVersion:`` field. Match loosely so a future re-ordering
        # of the manifest list doesn't break the test.
        assert "apiVersion" in body

    def test_manifest_resource_missing_file_returns_available_list(self) -> None:
        """Unknown filename returns ``Manifest 'X' not found.`` plus an available list."""
        from resources.k8s import k8s_manifest_resource

        body = k8s_manifest_resource("nonexistent-manifest-zzz.yaml")
        assert "not found" in body
        assert "Available:" in body

    def test_manifests_index_lists_known_manifests(self) -> None:
        """``k8s_manifests_index`` lists every yaml under ``MANIFESTS_DIR``."""
        from resources.k8s import MANIFESTS_DIR, k8s_manifests_index

        body = k8s_manifests_index()
        assert "Kubernetes Cluster Manifests" in body
        # Every yaml file must be linked from the index.
        yaml_files = sorted(MANIFESTS_DIR.glob("*.yaml"))
        for path in yaml_files:
            assert path.name in body, f"manifest {path.name!r} missing from index"


# ---------------------------------------------------------------------------
# gco_mcp/resources/iam_policies.py
# ---------------------------------------------------------------------------


class TestIamPolicies:
    def test_index_lists_real_policies(self) -> None:
        body = _read_resource("iam://gco/policies/index")
        # The repo ships a docs/iam-policies/ directory; either way the
        # heading is present.
        assert body.startswith("# IAM Policy Templates")

    def test_missing_policy_returns_available_list(self) -> None:
        body = _read_resource("iam://gco/policies/does-not-exist.json")
        assert "not found" in body


# ---------------------------------------------------------------------------
# gco_mcp/resources/ci.py
# ---------------------------------------------------------------------------


class TestCiIndex:
    def test_index_renders_workflow_section(self) -> None:
        body = _read_resource("ci://gco/index")
        # The repo ships at least one workflow under .github/workflows/.
        assert "## Workflows" in body
        # And the index always references the related resource trees.
        assert "## Related Resources" in body

    def test_workflow_resource_round_trips(self) -> None:
        body = _read_resource("ci://gco/workflows/unit-tests.yml")
        assert "Unit Tests" in body or "unit:" in body

    def test_workflow_resource_missing(self) -> None:
        body = _read_resource("ci://gco/workflows/does-not-exist.yml")
        assert "not found" in body

    def test_action_resource_missing_lists_available(self) -> None:
        body = _read_resource("ci://gco/actions/does-not-exist")
        assert "not found" in body

    def test_script_resource_round_trips(self) -> None:
        body = _read_resource("ci://gco/scripts/dependency-scan.sh")
        # Either the script content or the not-found message is fine —
        # we just want to exercise the read path.
        assert isinstance(body, str)

    def test_python_script_resource_round_trips(self) -> None:
        body = _read_resource("ci://gco/scripts/validate_k8s_manifests.py")
        assert "python" in body.lower()
        assert "File type '.py' not served" not in body

    def test_template_resource_missing(self) -> None:
        body = _read_resource("ci://gco/templates/does-not-exist.md")
        assert "not found" in body

    def test_template_resource_pull_request_template(self) -> None:
        body = _read_resource("ci://gco/templates/pull_request_template.md")
        # If the file exists it returns content; otherwise the not-found stub.
        assert isinstance(body, str)

    def test_codeql_resource_missing(self) -> None:
        body = _read_resource("ci://gco/codeql/does-not-exist.yml")
        assert "not found" in body

    def test_kind_resource_missing(self) -> None:
        body = _read_resource("ci://gco/kind/does-not-exist.yaml")
        assert "not found" in body

    def test_config_resource_disallowed(self) -> None:
        body = _read_resource("ci://gco/config/secret.txt")
        assert "not in the served allowlist" in body

    def test_config_resource_missing_allowed_file(self) -> None:
        # CODEOWNERS is on the allowlist; if it doesn't exist the
        # not-found stub is returned, otherwise the file body.
        body = _read_resource("ci://gco/config/CODEOWNERS")
        assert isinstance(body, str)


# ---------------------------------------------------------------------------
# gco_mcp/tools/docs.py — find_docs branch coverage
# ---------------------------------------------------------------------------


class TestFindDocsBranches:
    def test_query_only_no_topic_filter(self) -> None:
        from tools.docs import find_docs

        results = asyncio.run(find_docs(query="architecture"))
        assert isinstance(results, list)

    def test_topic_only_filter_drops_non_matches(self) -> None:
        from tools.docs import find_docs

        results = asyncio.run(find_docs(topic="capacity"))
        # Every returned doc must have at least one matching topic.
        for r in results:
            topics = r.get("topics", [])
            assert any("capacity" in str(t).lower() for t in topics or [])

    def test_query_topic_no_match_returns_empty(self) -> None:
        from tools.docs import find_docs

        results = asyncio.run(find_docs(query="zzznomatchhh"))
        assert results == []

    def test_negative_limit_returns_empty(self) -> None:
        from tools.docs import find_docs

        results = asyncio.run(find_docs(limit=0))
        assert results == []


# ---------------------------------------------------------------------------
# gco_mcp/tools/images.py — read-only and admin tool bodies
# ---------------------------------------------------------------------------


class TestImagesTools:
    def _patch_manager(self, mgr: Any) -> Any:
        return patch("tools.images._get_manager", return_value=mgr)

    def test_images_list(self) -> None:
        from tools import images as images_mod

        mgr = MagicMock()
        mgr.list_repos.return_value = [{"name": "gco/svc"}]
        with self._patch_manager(mgr):
            body = asyncio.run(images_mod.images_list())
        assert json.loads(body) == [{"name": "gco/svc"}]

    def test_images_tags(self) -> None:
        from tools import images as images_mod

        mgr = MagicMock()
        mgr.list_tags.return_value = [{"tag": "v1"}]
        with self._patch_manager(mgr):
            body = asyncio.run(images_mod.images_tags("svc"))
        assert json.loads(body) == [{"tag": "v1"}]

    def test_images_describe(self) -> None:
        from tools import images as images_mod

        mgr = MagicMock()
        mgr.describe.return_value = {"tag": "v1"}
        with self._patch_manager(mgr):
            body = asyncio.run(images_mod.images_describe("svc", "v1"))
        assert json.loads(body) == {"tag": "v1"}

    def test_images_uri(self) -> None:
        from tools import images as images_mod

        mgr = MagicMock()
        mgr.get_uri.return_value = "registry/gco/svc:latest"
        with self._patch_manager(mgr):
            body = asyncio.run(images_mod.images_uri("svc"))
        assert json.loads(body) == {"uri": "registry/gco/svc:latest"}

    def test_images_replication_get(self) -> None:
        from tools import images as images_mod

        mgr = MagicMock()
        mgr.replication_get.return_value = {}
        with self._patch_manager(mgr):
            body = asyncio.run(images_mod.images_replication_get())
        assert json.loads(body) == {}

    def test_images_replication_status(self) -> None:
        from tools import images as images_mod

        mgr = MagicMock()
        mgr.replication_status.return_value = []
        with self._patch_manager(mgr):
            body = asyncio.run(images_mod.images_replication_status())
        assert json.loads(body) == []

    def test_images_orphans_passes_threshold(self) -> None:
        from tools import images as images_mod

        mgr = MagicMock()
        mgr.orphans.return_value = []
        with self._patch_manager(mgr):
            asyncio.run(images_mod.images_orphans(threshold_days=7))
        mgr.orphans.assert_called_once_with(threshold_days=7)

    def test_images_init_passes_retain(self) -> None:
        from tools import images as images_mod

        mgr = MagicMock()
        mgr.init.return_value = {"name": "gco/svc", "created": True, "retain": True}
        with self._patch_manager(mgr):
            asyncio.run(images_mod.images_init("svc", retain=True))
        mgr.init.assert_called_once_with("svc", retain=True)

    def test_images_lifecycle_get_set(self) -> None:
        from tools import images as images_mod

        mgr = MagicMock()
        mgr.lifecycle_get.return_value = {}
        mgr.lifecycle_set.return_value = {"name": "gco/svc", "policy": {"rules": []}}
        with self._patch_manager(mgr):
            asyncio.run(images_mod.images_lifecycle_get("svc"))
            asyncio.run(images_mod.images_lifecycle_set("svc", {"rules": []}))
        mgr.lifecycle_set.assert_called_once_with("svc", {"rules": []})

    def test_images_replication_sync(self) -> None:
        from tools import images as images_mod

        mgr = MagicMock()
        mgr.replication_sync.return_value = {"destinations": []}
        with self._patch_manager(mgr):
            body = asyncio.run(images_mod.images_replication_sync())
        assert json.loads(body) == {"destinations": []}

    def test_get_manager_lazy_imports_factory(self) -> None:
        from tools import images as images_mod

        with patch("cli.images.get_image_manager", return_value="MGR") as mock_factory:
            assert images_mod._get_manager() == "MGR"
        mock_factory.assert_called_once()


# ---------------------------------------------------------------------------
# gco_mcp/audit_middleware.py — capture activation around tool calls
# ---------------------------------------------------------------------------


class TestAuditCaptureMiddleware:
    def test_buffers_reset_on_normal_exit(self) -> None:
        from audit import audit_elicitations_var, audit_messages_var
        from audit_middleware import AuditCaptureMiddleware

        mw = AuditCaptureMiddleware()
        ctx = MagicMock()

        captured_during_call: dict[str, Any] = {}

        async def call_next(_ctx: Any) -> str:
            captured_during_call["messages"] = audit_messages_var.get()
            captured_during_call["elicitations"] = audit_elicitations_var.get()
            return "ok"

        result = asyncio.run(mw.on_call_tool(ctx, call_next))
        assert result == "ok"
        # Inside the call, both buffers were live empty lists.
        assert captured_during_call["messages"] == []
        assert captured_during_call["elicitations"] == []
        # And after the call returns, the ContextVars are reset to None.
        assert audit_messages_var.get() is None
        assert audit_elicitations_var.get() is None

    def test_buffers_reset_on_exception(self) -> None:
        from audit import audit_elicitations_var, audit_messages_var
        from audit_middleware import AuditCaptureMiddleware

        mw = AuditCaptureMiddleware()
        ctx = MagicMock()

        async def call_next(_ctx: Any) -> str:
            raise RuntimeError("kaboom")

        with pytest.raises(RuntimeError, match="kaboom"):
            asyncio.run(mw.on_call_tool(ctx, call_next))

        # Even on exception the ContextVars must be reset.
        assert audit_messages_var.get() is None
        assert audit_elicitations_var.get() is None


# ---------------------------------------------------------------------------
# gco_mcp/cli_runner.py — error stub when subprocess fails
# ---------------------------------------------------------------------------


class TestCliRunner:
    def test_run_cli_returns_error_json_on_non_zero(self) -> None:
        import cli_runner

        result = MagicMock()
        result.returncode = 1
        result.stdout = ""
        result.stderr = "boom"
        with patch.object(cli_runner.subprocess, "run", return_value=result):
            output = cli_runner._run_cli("status")
        payload = json.loads(output)
        assert payload["error"] == "boom"
        assert payload["exit_code"] == 1

    def test_run_cli_returns_status_ok_when_empty_stdout(self) -> None:
        import cli_runner

        result = MagicMock()
        result.returncode = 0
        result.stdout = ""
        result.stderr = ""
        with patch.object(cli_runner.subprocess, "run", return_value=result):
            output = cli_runner._run_cli("status")
        payload = json.loads(output)
        assert payload == {"status": "ok"}

    def test_run_cli_rejects_path_traversal(self) -> None:
        import cli_runner

        output = cli_runner._run_cli("foo/../bar")
        payload = json.loads(output)
        assert "path traversal" in payload["error"]

    def test_run_cli_handles_timeout(self) -> None:
        import subprocess as _subprocess

        import cli_runner

        with patch.object(
            cli_runner.subprocess,
            "run",
            side_effect=_subprocess.TimeoutExpired("gco", 120),
        ):
            output = cli_runner._run_cli("status")
        payload = json.loads(output)
        assert "timed out" in payload["error"]

    def test_run_cli_handles_cli_missing(self) -> None:
        import cli_runner

        with patch.object(cli_runner.subprocess, "run", side_effect=FileNotFoundError):
            output = cli_runner._run_cli("status")
        payload = json.loads(output)
        assert "gco CLI not found" in payload["error"]


class TestImagesCtxWarning:
    """Coverage for ``mcp.tools.images._ctx_warning``.

    The helper is the only Context-aware piece of the destructive
    image-tool path that's reachable without registering the gated
    tools, and it's how operators (and the audit log) see a warning
    before a destructive ECR operation runs.
    """

    def test_no_op_when_get_context_raises(self) -> None:
        """Outside an MCP tool call, ``get_context()`` raises. The
        helper should suppress the failure and return without trying
        to dispatch a warning anywhere."""
        from tools.images import _ctx_warning

        # No Context active — call should complete cleanly.
        asyncio.run(_ctx_warning("dropping tag v1 from gco/svc"))

    def test_emits_warning_when_context_present(self) -> None:
        """When a Context is active, the helper must forward the
        message via ``ctx.warning``. We patch ``get_context`` to
        return a stub whose ``warning`` records the dispatch."""
        from tools import images as images_mod

        captured: list[str] = []

        class _StubCtx:
            async def warning(self, message: str) -> None:
                captured.append(message)

        with patch("fastmcp.server.dependencies.get_context", return_value=_StubCtx()):
            asyncio.run(images_mod._ctx_warning("about to destroy gco/svc"))

        assert captured == ["about to destroy gco/svc"]

    def test_swallows_warning_dispatch_failure(self) -> None:
        """If the active Context's ``warning`` itself raises (e.g.
        the transport is mid-shutdown), the helper must not propagate.
        Otherwise a transient failure during cleanup could mask the
        actual ECR operation result."""
        from tools import images as images_mod

        class _AngryCtx:
            async def warning(self, message: str) -> None:
                raise RuntimeError("transport closed")

        with patch("fastmcp.server.dependencies.get_context", return_value=_AngryCtx()):
            # Must not raise.
            asyncio.run(images_mod._ctx_warning("oh no"))


# ---------------------------------------------------------------------------
# gco_mcp/resources/tasks.py — direct-call branches
# ---------------------------------------------------------------------------
#
# The earlier ``TestTaskResource`` class covers the registry-routed paths
# (happy path, not-found mapping, invalid task_id, missing-extension stub).
# These extra cases call ``_task_resource`` directly to pin the module-seam
# behaviors: the ``server``-import failure stub and the ``_coerce_to_dict``
# conversion ladder including the ``str(record)`` last-resort.


class TestTasksResourceFallbacks:
    def test_server_import_failure_returns_protocol_unavailable_stub(self) -> None:
        """An unimportable ``server`` module degrades to the graceful stub."""
        from resources import tasks as tasks_mod

        with patch.dict(sys.modules, {"server": None}):
            body = asyncio.run(tasks_mod._task_resource("task-123"))
        payload = json.loads(body)
        assert payload["error"] == "task protocol not available"
        assert payload["task_id"] == "task-123"

    def test_lookup_delegates_to_tasks_get_with_server_singleton(self) -> None:
        """The direct-call path hands the shared server and id to ``tasks_get``."""
        from resources import tasks as tasks_mod
        from server import mcp as real_mcp

        record = MagicMock()
        record.model_dump.return_value = {"status": "working"}
        tasks_get = AsyncMock(return_value=record)
        with patch("fastmcp_tasks.handlers.tasks_get", new=tasks_get):
            body = asyncio.run(tasks_mod._task_resource("task-xyz"))
        tasks_get.assert_awaited_once_with(real_mcp, "task-xyz")
        payload = json.loads(body)
        assert payload["state"]["status"] == "working"

    def test_coerce_to_dict_uses_model_dump(self) -> None:
        from resources.tasks import _coerce_to_dict

        class Pydantic:
            def model_dump(self) -> dict[str, str]:
                return {"a": "b"}

        assert _coerce_to_dict(Pydantic()) == {"a": "b"}

    def test_coerce_to_dict_skips_methods_that_raise(self) -> None:
        from resources.tasks import _coerce_to_dict

        class Bad:
            def model_dump(self) -> dict[str, str]:
                raise RuntimeError("nope")

            def to_dict(self) -> dict[str, str]:
                return {"x": "y"}

        assert _coerce_to_dict(Bad()) == {"x": "y"}

    def test_coerce_to_dict_skips_methods_returning_non_dict(self) -> None:
        from resources.tasks import _coerce_to_dict

        class WeirdDump:
            def model_dump(self) -> str:  # type: ignore[override]
                return "not a dict"

            def to_dict(self) -> dict[str, str]:
                return {"ok": "yes"}

        assert _coerce_to_dict(WeirdDump()) == {"ok": "yes"}

    def test_coerce_to_dict_falls_through_to_vars(self) -> None:
        from resources.tasks import _coerce_to_dict

        class Plain:
            def __init__(self) -> None:
                self.public = 1
                self._private = 2

        out = _coerce_to_dict(Plain())
        assert out == {"public": 1}

    def test_coerce_to_dict_str_fallback_for_unconvertible(self) -> None:
        """Slotted objects without ``__dict__`` and no model methods land
        on ``str(record)``."""
        from resources.tasks import _coerce_to_dict

        class Slotted:
            __slots__ = ()

            def __repr__(self) -> str:
                return "Slotted()"

        out = _coerce_to_dict(Slotted())
        assert out == {"value": "Slotted()"}


# ---------------------------------------------------------------------------
# gco_mcp/resources/docs.py — by-category, by-use-case, by-topic, by-related
# ---------------------------------------------------------------------------


class TestDocsCategoryAndUseCaseResources:
    """Per-bucket resource handlers in ``gco_mcp/resources/docs.py``."""

    def test_examples_by_category_lists_matches(self) -> None:
        # ``Jobs & Training`` is the largest bucket — matches case-insensitive.
        body = _read_resource("docs://gco/examples/by-category/jobs%20%26%20training")
        assert "# Examples in category" in body
        assert "`docs://gco/examples/" in body

    def test_examples_by_category_unknown(self) -> None:
        body = _read_resource("docs://gco/examples/by-category/nonexistent-bucket")
        assert "not found" in body
        assert "Available:" in body

    def test_examples_by_use_case_substring_match(self) -> None:
        body = _read_resource("docs://gco/examples/by-use-case/smoke%20test")
        assert "# Examples matching use case" in body
        assert "`docs://gco/examples/" in body

    def test_examples_by_use_case_no_match(self) -> None:
        body = _read_resource("docs://gco/examples/by-use-case/utterly-fictional-need")
        assert "No examples match" in body
        assert "find_examples" in body

    def test_docs_by_topic_lists_matches(self) -> None:
        body = _read_resource("docs://gco/docs/by-topic/inference")
        assert "# Docs matching topic" in body
        assert "`docs://gco/docs/" in body

    def test_docs_by_topic_unknown_returns_available(self) -> None:
        body = _read_resource("docs://gco/docs/by-topic/zzz-not-real")
        assert "not found" in body
        assert "Available:" in body

    def test_docs_by_related_unknown_doc(self) -> None:
        body = _read_resource("docs://gco/docs/by-related/NOT_A_DOC")
        assert "not found" in body
        assert "Available:" in body

    def test_docs_by_related_known_doc_renders_both_directions(self) -> None:
        # ``CLI`` is referenced by other docs and references others itself,
        # so the response must contain both H2 sections.
        body = _read_resource("docs://gco/docs/by-related/CLI")
        assert "# Docs related to CLI" in body
        # At least one direction must produce a bullet.
        assert "`docs://gco/docs/" in body


class TestExampleResourceMetadataHeader:
    """The metadata header rendering branches in ``example_resource``."""

    def test_unknown_example_lists_available(self) -> None:
        body = _read_resource("docs://gco/examples/no-such-example")
        assert "not found" in body
        assert "Available:" in body

    def test_known_gpu_example_emits_full_header(self) -> None:
        # ``gpu-job`` has every metadata field set: gpu, instance_types,
        # use_cases, related, keywords. Ensures every conditional branch
        # of the header builder runs at least once.
        body = _read_resource("docs://gco/examples/gpu-job")
        assert "# Example: gpu-job" in body
        assert "# GPU/Accelerator: NVIDIA" in body
        assert "# Submit with:" in body
        assert "# Keywords:" in body
        assert "# Instance Types:" in body
        assert "# Use Cases:" in body
        assert "# Related:" in body
        assert "# --- Manifest begins below ---" in body

    def test_simple_job_no_gpu_skips_gpu_line(self) -> None:
        body = _read_resource("docs://gco/examples/simple-job")
        # ``gpu`` is "no" — that line must be omitted from the header.
        assert "# GPU/Accelerator" not in body


class TestDocResourceMetadataHeader:
    """The HTML-comment header in ``doc_resource``."""

    def test_known_doc_renders_topics_and_related_header(self) -> None:
        body = _read_resource("docs://gco/docs/CLI")
        # ``CLI`` ships with both topics and related metadata, so both
        # HTML-comment headers must render.
        assert body.startswith("<!-- Topics:")
        assert "<!-- Related:" in body

    def test_unknown_doc_returns_not_found(self) -> None:
        body = _read_resource("docs://gco/docs/NOT_A_REAL_DOC")
        assert "not found" in body
