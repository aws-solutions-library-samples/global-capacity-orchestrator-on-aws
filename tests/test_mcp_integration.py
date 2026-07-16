"""
Integration tests for the MCP server through the FastMCP protocol layer.

Unlike test_mcp_server.py which mocks subprocess.run, this suite runs
the real FastMCP server end-to-end via two transports: an in-process
Client(run_mcp.mcp) for fast checks and a stdio Client(StdioTransport)
for the marked-slow full subprocess path. Verifies tool discovery,
input-schema shape (every tool has a description, recommend_region
has instance_type/gpu_count/gpu parameters, and so on), call round
trips, and error propagation back through the protocol.
"""

import json
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

# Ensure gco_mcp/ is importable
sys.path.insert(0, str(Path(__file__).parent.parent / "gco_mcp"))

import run_mcp

PROJECT_ROOT = Path(__file__).parent.parent


# ---------------------------------------------------------------------------
# In-process protocol tests (fast — no subprocess)
# ---------------------------------------------------------------------------


class TestMCPProtocolTools:
    """Test tool discovery and invocation through the MCP protocol layer."""

    @pytest.mark.asyncio
    async def test_list_tools_returns_all_registered_tools(self):
        """Server should expose all registered tools via the underlying registry.

        ``client.list_tools()`` goes through the BM25/Code-Mode catalog-replacement
        transforms and returns the always-visible entry-point set plus the
        synthetic ``search_tools`` / ``read_resource`` tools — that's the
        client-facing surface and is covered by ``test_mcp_transforms.py``.
        For the registration check we want the underlying registry, which is
        what ``mcp._list_tools()`` returns.
        """
        tools = await run_mcp.mcp._list_tools()
        tool_names = {t.name for t in tools}

        expected = {
            "list_jobs",
            "submit_job_sqs",
            "check_capacity",
            "recommend_region",
            "spot_prices",
            "deploy_inference",
            "cost_summary",
            "list_stacks",
            "list_models",
            "list_storage_contents",
        }
        assert expected.issubset(tool_names), f"Missing tools: {expected - tool_names}"
        assert len(tools) >= 35

    @pytest.mark.asyncio
    async def test_tool_schemas_have_descriptions(self):
        """Every tool should have a description for LLM consumption."""
        tools = await run_mcp.mcp._list_tools()
        for tool in tools:
            assert tool.description, f"Tool {tool.name} has no description"

    @pytest.mark.asyncio
    async def test_recommend_region_tool_has_instance_type_param(self):
        """The recommend_region tool should accept instance_type and gpu_count."""
        tools = await run_mcp.mcp._list_tools()
        rec_tool = next(t for t in tools if t.name == "recommend_region")
        # Tool registry exposes the function via ``tool.parameters`` (FastMCP's
        # canonical schema source) when the public ``inputSchema`` field is
        # transformed by the BM25 catalog replacement.
        schema = getattr(rec_tool, "parameters", None) or getattr(rec_tool, "inputSchema", {})
        props = schema.get("properties", {})
        assert "instance_type" in props
        assert "gpu_count" in props
        assert "gpu" in props

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_call_tool_round_trip(self):
        """Tool call should serialize/deserialize properly over the protocol."""
        from fastmcp import Client

        async with Client(run_mcp.mcp) as client:
            with patch("cli_runner._run_cli", return_value='{"status": "ok"}'):
                result = await client.call_tool("list_stacks", {}, raise_on_error=False)
                assert result is not None
                text = result.content[0].text if result.content else ""
                assert "ok" in text

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_call_tool_with_arguments(self):
        """Tool call with arguments should pass them through correctly."""
        from fastmcp import Client

        async with Client(run_mcp.mcp) as client:
            with patch("cli_runner._run_cli") as mock_cli:
                mock_cli.return_value = '{"region": "us-east-1"}'
                await client.call_tool(
                    "check_capacity",
                    {"instance_type": "g5.xlarge", "region": "us-east-1"},
                    raise_on_error=False,
                )
                mock_cli.assert_called_once()
                args = mock_cli.call_args[0]
                assert "capacity" in args
                assert "g5.xlarge" in args
                assert "us-east-1" in args

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_call_tool_recommend_region_with_instance_type(self):
        """recommend_region should pass instance_type and gpu_count through."""
        from fastmcp import Client

        async with Client(run_mcp.mcp) as client:
            with patch("cli_runner._run_cli") as mock_cli:
                mock_cli.return_value = '{"region": "us-west-2"}'
                await client.call_tool(
                    "recommend_region",
                    {"instance_type": "p4d.24xlarge", "gpu_count": 8},
                    raise_on_error=False,
                )
                args = mock_cli.call_args[0]
                assert "p4d.24xlarge" in args
                assert "--gpu-count" in args
                assert "8" in args

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_call_unknown_tool_raises(self):
        """Calling a non-existent tool should raise an error."""
        from fastmcp import Client

        async with Client(run_mcp.mcp) as client:
            from fastmcp.exceptions import ToolError

            with pytest.raises(ToolError):
                await client.call_tool("nonexistent_tool", {}, raise_on_error=True)

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_call_tool_with_cli_error(self):
        """Tool should return error JSON when the CLI fails."""
        from fastmcp import Client

        async with Client(run_mcp.mcp) as client:
            with patch("cli_runner._run_cli") as mock_cli:
                mock_cli.return_value = '{"error": "Stack not found", "exit_code": 1}'
                result = await client.call_tool(
                    "stack_status",
                    {"stack_name": "bad-stack", "region": "us-east-1"},
                    raise_on_error=False,
                )
                text = result.content[0].text if result.content else ""
                assert "error" in text

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_call_spot_prices_tool(self):
        """spot_prices tool should pass instance_type and region correctly."""
        from fastmcp import Client

        async with Client(run_mcp.mcp) as client:
            with patch("cli_runner._run_cli") as mock_cli:
                mock_cli.return_value = '{"prices": []}'
                await client.call_tool(
                    "spot_prices",
                    {"instance_type": "g4dn.xlarge", "region": "us-west-2"},
                    raise_on_error=False,
                )
                args = mock_cli.call_args[0]
                assert "spot-prices" in args
                assert "g4dn.xlarge" in args
                assert "us-west-2" in args

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_call_capacity_status_tool(self):
        """capacity_status tool should work with and without region."""
        from fastmcp import Client

        async with Client(run_mcp.mcp) as client:
            with patch("cli_runner._run_cli") as mock_cli:
                mock_cli.return_value = '{"regions": []}'
                await client.call_tool("capacity_status", {}, raise_on_error=False)
                args = mock_cli.call_args[0]
                assert "status" in args

                mock_cli.reset_mock()
                await client.call_tool(
                    "capacity_status",
                    {"region": "eu-west-1"},
                    raise_on_error=False,
                )
                args = mock_cli.call_args[0]
                assert "eu-west-1" in args


class TestMCPProtocolResources:
    """Test resource discovery and reading through the MCP protocol layer."""

    @pytest.mark.asyncio
    async def test_list_resources_returns_registered_resources(self):
        """Server should expose registered resources via list_resources."""
        from fastmcp import Client

        async with Client(run_mcp.mcp) as client:
            resources = await client.list_resources()
            uris = {str(r.uri) for r in resources}
            assert any("docs://gco/index" in u for u in uris)
            assert any("docs://gco/README" in u for u in uris)
            assert any("source://gco/index" in u for u in uris)

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_read_docs_index(self):
        """Reading docs://gco/index should return the documentation index."""
        from fastmcp import Client

        async with Client(run_mcp.mcp) as client:
            result = await client.read_resource("docs://gco/index")
            text = result[0].text if result else ""
            assert "Resource Index" in text
            assert "docs://gco/docs/" in text

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_read_readme_resource(self):
        """Reading docs://gco/README should return the project README."""
        from fastmcp import Client

        async with Client(run_mcp.mcp) as client:
            result = await client.read_resource("docs://gco/README")
            text = result[0].text if result else ""
            assert "GCO" in text
            assert len(text) > 100

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_read_doc_resource(self):
        """Reading a specific doc should return its content."""
        from fastmcp import Client

        async with Client(run_mcp.mcp) as client:
            result = await client.read_resource("docs://gco/docs/CLI")
            text = result[0].text if result else ""
            assert "capacity" in text.lower()
            assert "recommend-region" in text

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_read_example_resource(self):
        """Reading an example manifest should return YAML content."""
        from fastmcp import Client

        async with Client(run_mcp.mcp) as client:
            result = await client.read_resource("docs://gco/examples/simple-job")
            text = result[0].text if result else ""
            assert "kind:" in text or "apiVersion:" in text

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_read_source_index(self):
        """Reading source://gco/index should list source files."""
        from fastmcp import Client

        async with Client(run_mcp.mcp) as client:
            result = await client.read_resource("source://gco/index")
            text = result[0].text if result else ""
            assert "source://gco/file/" in text
            assert "capacity" in text

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_read_source_file(self):
        """Reading a source file should return its content."""
        from fastmcp import Client

        async with Client(run_mcp.mcp) as client:
            result = await client.read_resource("source://gco/file/cli/capacity/__init__.py")
            text = result[0].text if result else ""
            assert "compute_weighted_score" in text

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_read_config_resource(self):
        """Reading a config file should return its content."""
        from fastmcp import Client

        async with Client(run_mcp.mcp) as client:
            result = await client.read_resource("source://gco/config/pyproject.toml")
            text = result[0].text if result else ""
            assert "gco" in text.lower()

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_read_nonexistent_doc_returns_error_message(self):
        """Reading a non-existent doc should return a helpful error."""
        from fastmcp import Client

        async with Client(run_mcp.mcp) as client:
            result = await client.read_resource("docs://gco/docs/NONEXISTENT")
            text = result[0].text if result else ""
            assert "not found" in text.lower()
            assert "Available" in text

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_read_disallowed_config_returns_error(self):
        """Reading a non-allowed config file should be denied."""
        from fastmcp import Client

        async with Client(run_mcp.mcp) as client:
            result = await client.read_resource("source://gco/config/secrets.yaml")
            text = result[0].text if result else ""
            assert "not available" in text.lower()

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_read_tests_index(self):
        """Reading tests://gco/index should list test files."""
        from fastmcp import Client

        async with Client(run_mcp.mcp) as client:
            result = await client.read_resource("tests://gco/index")
            text = result[0].text if result else ""
            assert "Test Suite" in text
            assert "test_mcp_server" in text

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_read_tests_readme(self):
        """Reading tests://gco/README should return the test suite docs."""
        from fastmcp import Client

        async with Client(run_mcp.mcp) as client:
            result = await client.read_resource("tests://gco/README")
            text = result[0].text if result else ""
            assert "GCO Test Suite" in text

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_read_config_index(self):
        """Reading config://gco/index should list configuration resources."""
        from fastmcp import Client

        async with Client(run_mcp.mcp) as client:
            result = await client.read_resource("config://gco/index")
            text = result[0].text if result else ""
            assert "config://gco/cdk.json" in text
            assert "mcp://gco/feature-flags" in text
            assert "config://gco/feature-toggles" not in text

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_read_config_cdk_json(self):
        """Reading config://gco/cdk.json should return the CDK config."""
        from fastmcp import Client

        async with Client(run_mcp.mcp) as client:
            result = await client.read_resource("config://gco/cdk.json")
            text = result[0].text if result else ""
            assert "{" in text
            assert len(text) > 100

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_read_mcp_feature_flags(self):
        """Reading mcp://gco/feature-flags should return the live gating map."""
        from fastmcp import Client

        async with Client(run_mcp.mcp) as client:
            result = await client.read_resource("mcp://gco/feature-flags")
            text = result[0].text if result else ""
            payload = json.loads(text)
            names = {item["name"] for item in payload["flags"]}
            assert "GCO_ENABLE_MODEL_UPLOAD" in names

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_read_config_env_vars(self):
        """Reading config://gco/env-vars should list environment variables."""
        from fastmcp import Client

        async with Client(run_mcp.mcp) as client:
            result = await client.read_resource("config://gco/env-vars")
            text = result[0].text if result else ""
            assert "GCO_MCP_ROLE_ARN" in text

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_read_examples_guide(self):
        """Reading docs://gco/examples/guide should return the manifest guide."""
        from fastmcp import Client

        async with Client(run_mcp.mcp) as client:
            result = await client.read_resource("docs://gco/examples/guide")
            text = result[0].text if result else ""
            assert "Example Manifest Guide" in text
            assert "simple-job" in text
            assert "Security Context" in text

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_example_resource_includes_metadata_header(self):
        """Example resources should include metadata before the YAML."""
        from fastmcp import Client

        async with Client(run_mcp.mcp) as client:
            result = await client.read_resource("docs://gco/examples/gpu-job")
            text = result[0].text if result else ""
            assert "# Example: gpu-job" in text
            assert "# Category:" in text
            assert "apiVersion:" in text

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_docs_index_references_new_resource_groups(self):
        """The docs index should cross-reference tests:// and config://."""
        from fastmcp import Client

        async with Client(run_mcp.mcp) as client:
            result = await client.read_resource("docs://gco/index")
            text = result[0].text if result else ""
            assert "tests://gco/index" in text
            assert "config://gco/index" in text
            assert "docs://gco/examples/guide" in text

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_read_k8s_manifests_index(self):
        """Reading k8s://gco/manifests/index should list cluster manifests."""
        from fastmcp import Client

        async with Client(run_mcp.mcp) as client:
            result = await client.read_resource("k8s://gco/manifests/index")
            text = result[0].text if result else ""
            assert "Kubernetes Cluster Manifests" in text
            assert "k8s://gco/manifests/" in text

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_read_iam_policies_index(self):
        """Reading iam://gco/policies/index should list IAM policy templates."""
        from fastmcp import Client

        async with Client(run_mcp.mcp) as client:
            result = await client.read_resource("iam://gco/policies/index")
            text = result[0].text if result else ""
            assert "IAM Policy Templates" in text

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_read_ci_index(self):
        """Reading ci://gco/index should list CI/CD artefacts."""
        from fastmcp import Client

        async with Client(run_mcp.mcp) as client:
            result = await client.read_resource("ci://gco/index")
            text = result[0].text if result else ""
            assert "GitHub Actions" in text
            assert "ci://gco/workflows/" in text


class TestMCPProtocolToolSchemas:
    """Test that tool input schemas are well-formed for LLM consumption."""

    @pytest.mark.asyncio
    async def test_all_tools_have_input_schemas(self):
        """Every tool should have a non-empty inputSchema."""
        tools = await run_mcp.mcp._list_tools()
        for tool in tools:
            schema = getattr(tool, "parameters", None) or getattr(tool, "inputSchema", None)
            assert schema is not None, f"Tool {tool.name} has no input schema"

    @pytest.mark.asyncio
    async def test_deploy_inference_has_required_params(self):
        """deploy_inference should require name and image."""
        tools = await run_mcp.mcp._list_tools()
        deploy = next(t for t in tools if t.name == "deploy_inference")
        schema = getattr(deploy, "parameters", None) or getattr(deploy, "inputSchema", {})
        required = schema.get("required", [])
        assert "name" in required
        assert "image" in required

    @pytest.mark.asyncio
    async def test_submit_job_sqs_has_required_params(self):
        """submit_job_sqs should require manifest_path and region."""
        tools = await run_mcp.mcp._list_tools()
        submit = next(t for t in tools if t.name == "submit_job_sqs")
        schema = getattr(submit, "parameters", None) or getattr(submit, "inputSchema", {})
        required = schema.get("required", [])
        assert "manifest_path" in required
        assert "region" in required

    @pytest.mark.asyncio
    async def test_check_capacity_has_required_params(self):
        """check_capacity should require instance_type and region."""
        tools = await run_mcp.mcp._list_tools()
        check = next(t for t in tools if t.name == "check_capacity")
        schema = getattr(check, "parameters", None) or getattr(check, "inputSchema", {})
        required = schema.get("required", [])
        assert "instance_type" in required
        assert "region" in required


# ---------------------------------------------------------------------------
# Stdio subprocess tests (true integration — marked slow)
# ---------------------------------------------------------------------------


@pytest.mark.slow
@pytest.mark.integration
class TestMCPStdioProtocol:
    """Test the MCP server over actual stdio subprocess transport.

    These tests start the server as a real subprocess and communicate
    over stdin/stdout using the MCP protocol. This validates the full
    stack including process startup, protocol negotiation, and shutdown.
    """

    @pytest.mark.asyncio
    async def test_stdio_server_starts_and_lists_tools(self):
        """Server should start over stdio and respond to list_tools.

        Under the default ``GCO_MCP_TOOL_SEARCH=bm25`` the public listing is
        the BM25 always-visible set plus the synthetic ``search_tools`` /
        ``read_resource`` tools. We assert the always-visible entries plus
        the search synthetic so the test confirms the stdio transport is
        wired correctly without hard-coding a tool count that the search
        transform will keep moving.
        """
        from fastmcp import Client
        from fastmcp.client.transports import StdioTransport

        transport = StdioTransport(
            command=sys.executable,
            args=[str(PROJECT_ROOT / "gco_mcp" / "run_mcp.py")],
            cwd=str(PROJECT_ROOT),
        )
        async with Client(transport) as client:
            tools = await client.list_tools()
            tool_names = {t.name for t in tools}
            assert "list_jobs" in tool_names
            assert "search_tools" in tool_names
            assert "read_resource" in tool_names

    @pytest.mark.asyncio
    async def test_stdio_server_lists_resources(self):
        """Server should list resources over stdio."""
        from fastmcp import Client
        from fastmcp.client.transports import StdioTransport

        transport = StdioTransport(
            command=sys.executable,
            args=[str(PROJECT_ROOT / "gco_mcp" / "run_mcp.py")],
            cwd=str(PROJECT_ROOT),
        )
        async with Client(transport) as client:
            resources = await client.list_resources()
            uris = {str(r.uri) for r in resources}
            assert any("docs://gco/index" in u for u in uris)
            assert any("source://gco/index" in u for u in uris)

    @pytest.mark.asyncio
    async def test_stdio_server_reads_resource(self):
        """Server should serve resource content over stdio."""
        from fastmcp import Client
        from fastmcp.client.transports import StdioTransport

        transport = StdioTransport(
            command=sys.executable,
            args=[str(PROJECT_ROOT / "gco_mcp" / "run_mcp.py")],
            cwd=str(PROJECT_ROOT),
        )
        async with Client(transport) as client:
            result = await client.read_resource("docs://gco/README")
            text = result[0].text if result else ""
            assert "GCO" in text

    @pytest.mark.asyncio
    async def test_stdio_tool_call_serialization(self):
        """Tool calls should serialize/deserialize correctly over stdio."""
        from fastmcp import Client
        from fastmcp.client.transports import StdioTransport

        transport = StdioTransport(
            command=sys.executable,
            args=[str(PROJECT_ROOT / "gco_mcp" / "run_mcp.py")],
            cwd=str(PROJECT_ROOT),
        )
        async with Client(transport) as client:
            # Use capacity_status instead of list_stacks — it doesn't
            # shell out to cdk and returns a JSON result even without
            # live infrastructure.
            result = await client.call_tool("capacity_status", {}, raise_on_error=False)
            assert result is not None
            assert len(result.content) > 0
