"""
Tests for the new MCP resource groups added during the refactor:
- tests:// — Test suite documentation and patterns
- config:// — CDK configuration, feature toggles, environment variables
- docs://gco/examples/guide — Example manifest creation guide
- Enhanced example metadata in docs://gco/examples/{name}
"""

import asyncio
import json
import sys
from pathlib import Path

# Ensure gco_mcp/ is importable
sys.path.insert(0, str(Path(__file__).parent.parent / "gco_mcp"))

import run_mcp


class TestTestsResources:
    """Tests for the tests:// resource group."""

    def test_tests_index_contains_readme(self):
        result = asyncio.run(run_mcp.mcp.read_resource("tests://gco/index"))
        content = result.contents[0].content
        assert "Test Suite" in content
        assert "tests://gco/README" in content

    def test_tests_index_contains_test_files(self):
        result = asyncio.run(run_mcp.mcp.read_resource("tests://gco/index"))
        content = result.contents[0].content
        assert "## Test Files" in content
        assert "test_mcp_server" in content

    def test_tests_index_contains_infrastructure(self):
        result = asyncio.run(run_mcp.mcp.read_resource("tests://gco/index"))
        content = result.contents[0].content
        assert "## Test Infrastructure" in content
        assert "conftest" in content or "_cdk_config_matrix" in content

    def test_tests_index_contains_bats(self):
        result = asyncio.run(run_mcp.mcp.read_resource("tests://gco/index"))
        content = result.contents[0].content
        assert "BATS" in content

    def test_tests_reads_readme(self):
        result = asyncio.run(run_mcp.mcp.read_resource("tests://gco/README"))
        content = result.contents[0].content
        assert "GCO Test Suite" in content
        assert len(content) > 100

    def test_tests_reads_test_file(self):
        result = asyncio.run(run_mcp.mcp.read_resource("tests://gco/test_mcp_server.py"))
        content = result.contents[0].content
        assert "import" in content
        assert len(content) > 100

    def test_tests_reads_conftest(self):
        result = asyncio.run(run_mcp.mcp.read_resource("tests://gco/conftest.py"))
        content = result.contents[0].content
        assert len(content) > 10

    def test_tests_reads_bats_readme(self):
        result = asyncio.run(run_mcp.mcp.read_resource("tests://gco/BATS/README.md"))
        content = result.contents[0].content
        assert len(content) > 10

    def test_tests_missing_file(self):
        result = asyncio.run(run_mcp.mcp.read_resource("tests://gco/nonexistent.py"))
        content = result.contents[0].content
        assert "not found" in content


class TestConfigResources:
    """Tests for the config:// resource group."""

    def test_config_index_contains_sections(self):
        result = asyncio.run(run_mcp.mcp.read_resource("config://gco/index"))
        content = result.contents[0].content
        assert "Configuration" in content
        assert "config://gco/cdk.json" in content
        assert "mcp://gco/feature-flags" in content
        assert "config://gco/feature-toggles" not in content
        assert "config://gco/env-vars" in content

    def test_config_cdk_json_returns_content(self):
        result = asyncio.run(run_mcp.mcp.read_resource("config://gco/cdk.json"))
        content = result.contents[0].content
        assert len(content) > 100
        # cdk.json should contain JSON
        assert "{" in content

    def test_mcp_feature_flags_returns_current_gating_map(self):
        result = asyncio.run(run_mcp.mcp.read_resource("mcp://gco/feature-flags"))
        payload = json.loads(result.contents[0].content)
        flags = {item["name"]: item for item in payload["flags"]}
        assert "GCO_ENABLE_MODEL_UPLOAD" in flags
        assert {
            "models_upload",
            "upload_to_regional_bucket",
        }.issubset(flags["GCO_ENABLE_MODEL_UPLOAD"]["gated_tools"])

    def test_config_env_vars_returns_content(self):
        result = asyncio.run(run_mcp.mcp.read_resource("config://gco/env-vars"))
        content = result.contents[0].content
        assert "Environment Variables" in content
        assert "GCO_MCP_ROLE_ARN" in content
        assert "GCO_ENABLE_CAPACITY_PURCHASE" in content


class TestExamplesGuideResource:
    """Tests for the docs://gco/examples/guide resource."""

    def test_guide_contains_metadata_table(self):
        result = asyncio.run(run_mcp.mcp.read_resource("docs://gco/examples/guide"))
        content = result.contents[0].content
        assert "Example Manifest Guide" in content
        assert "| Example |" in content
        assert "simple-job" in content
        assert "gpu-job" in content

    def test_guide_contains_common_patterns(self):
        result = asyncio.run(run_mcp.mcp.read_resource("docs://gco/examples/guide"))
        content = result.contents[0].content
        assert "## Common Patterns" in content
        assert "Security Context" in content
        assert "GPU Resources" in content
        assert "EFS Shared Storage" in content

    def test_guide_contains_submission_methods(self):
        result = asyncio.run(run_mcp.mcp.read_resource("docs://gco/examples/guide"))
        content = result.contents[0].content
        assert "Submission Methods" in content
        assert "SQS" in content
        assert "API Gateway" in content

    def test_guide_covers_all_examples(self):
        """Every .yaml file in examples/ should appear in the guide."""
        result = asyncio.run(run_mcp.mcp.read_resource("docs://gco/examples/guide"))
        content = result.contents[0].content
        examples_dir = Path(__file__).parent.parent / "examples"
        for f in examples_dir.glob("*.yaml"):
            assert f.stem in content, f"Example {f.stem} missing from guide"


class TestEnhancedExampleResources:
    """Tests for the enhanced example resources with metadata headers."""

    def test_example_resource_has_metadata_header(self):
        result = asyncio.run(run_mcp.mcp.read_resource("docs://gco/examples/simple-job"))
        content = result.contents[0].content
        assert "# Example: simple-job" in content
        assert "# Category:" in content
        assert "# Summary:" in content
        assert "# Submit with:" in content

    def test_example_resource_has_gpu_info_when_applicable(self):
        result = asyncio.run(run_mcp.mcp.read_resource("docs://gco/examples/gpu-job"))
        content = result.contents[0].content
        assert "# GPU/Accelerator:" in content

    def test_example_resource_has_opt_in_when_applicable(self):
        result = asyncio.run(run_mcp.mcp.read_resource("docs://gco/examples/fsx-lustre-job"))
        content = result.contents[0].content
        assert "# Opt-in required:" in content

    def test_example_resource_contains_manifest(self):
        result = asyncio.run(run_mcp.mcp.read_resource("docs://gco/examples/simple-job"))
        content = result.contents[0].content
        # Should contain the actual YAML manifest after the header
        assert "apiVersion:" in content
        assert "kind:" in content

    def test_all_examples_have_metadata(self):
        """Every .yaml file in examples/ should have metadata in EXAMPLE_METADATA."""
        from resources.docs import EXAMPLE_METADATA

        examples_dir = Path(__file__).parent.parent / "examples"
        for f in examples_dir.glob("*.yaml"):
            assert f.stem in EXAMPLE_METADATA, (
                f"Example {f.stem} missing from EXAMPLE_METADATA in gco_mcp/resources/docs.py"
            )


class TestDocsIndexNewGroups:
    """Tests that the docs index references the new resource groups."""

    def test_docs_index_references_tests_group(self):
        result = asyncio.run(run_mcp.mcp.read_resource("docs://gco/index"))
        content = result.contents[0].content
        assert "tests://gco/index" in content

    def test_docs_index_references_config_group(self):
        result = asyncio.run(run_mcp.mcp.read_resource("docs://gco/index"))
        content = result.contents[0].content
        assert "config://gco/index" in content

    def test_docs_index_references_examples_guide(self):
        result = asyncio.run(run_mcp.mcp.read_resource("docs://gco/index"))
        content = result.contents[0].content
        assert "docs://gco/examples/guide" in content

    def test_docs_index_categorizes_all_examples(self):
        """Every example category from EXAMPLE_METADATA should appear in the index."""
        result = asyncio.run(run_mcp.mcp.read_resource("docs://gco/index"))
        content = result.contents[0].content
        expected_categories = {
            "Jobs & Training",
            "Accelerator Jobs",
            "Inference Serving",
            "Storage & Persistence",
            "Caching & Databases",
            "Schedulers",
            "Distributed Computing",
            "DAG Pipelines",
        }
        for cat in expected_categories:
            assert cat in content, f"Category '{cat}' missing from docs index"


class TestModuleStructure:
    """Tests that the refactored module structure is correct."""

    def test_tools_are_importable(self):
        """Every tool module imports and contributes registered tools to the server."""
        from tools import capacity, costs, inference, jobs, models, stacks, storage

        registered = asyncio.run(run_mcp.mcp._list_tools())
        by_module = {tool.fn.__module__ for tool in registered if getattr(tool, "fn", None)}
        for module in (capacity, costs, inference, jobs, models, stacks, storage):
            assert module.__name__ in by_module, f"{module.__name__} registered no tools"

    def test_resources_are_importable(self):
        """Every resource module imports and registers at least one resource or template."""
        from resources import (
            ci,
            clients,
            config,
            demos,
            docs,
            iam_policies,
            infra,
            k8s,
            scripts,
            source,
            tests,
        )

        registered = list(asyncio.run(run_mcp.mcp._list_resources())) + list(
            asyncio.run(run_mcp.mcp._list_resource_templates())
        )
        by_module = {item.fn.__module__ for item in registered if getattr(item, "fn", None)}
        for module in (
            ci,
            clients,
            config,
            demos,
            docs,
            iam_policies,
            infra,
            k8s,
            scripts,
            source,
            tests,
        ):
            assert module.__name__ in by_module, f"{module.__name__} registered no resources"

    def test_audit_module_importable(self):
        """The audit helpers import and redact secrets rather than echoing them."""
        from audit import _sanitize_arguments, audit_logged

        assert callable(audit_logged)
        sanitized = _sanitize_arguments({"password": "hunter2", "region": "us-east-1"})
        assert sanitized["region"] == "us-east-1"
        assert sanitized["password"] != "hunter2"

    def test_cli_runner_importable(self):
        """The CLI runner seam is an ordinary callable the tools can patch."""
        import inspect

        from cli_runner import _run_cli

        assert callable(_run_cli)
        assert "args" in inspect.signature(_run_cli).parameters

    def test_version_module_importable(self):
        from version import get_project_version

        assert get_project_version() != "unknown"


class TestEnvVarsResourceMatchesTheCode:
    """``config://gco/env-vars`` lists what the code reads, and only that."""

    @staticmethod
    def _listed() -> set[str]:
        import re

        content = (
            asyncio.run(run_mcp.mcp.read_resource("config://gco/env-vars")).contents[0].content
        )
        rows = re.findall(r"^\| (`[A-Z][A-Z0-9_]+`(?: / `[A-Z][A-Z0-9_]+`)*) \|", content, re.M)
        return {name for row in rows for name in re.findall(r"`([A-Z0-9_]+)`", row)}

    @staticmethod
    def _sources() -> dict[str, str]:
        import subprocess

        root = Path(__file__).resolve().parent.parent
        names = subprocess.run(
            ["git", "ls-files", "cli", "gco", "gco_mcp", "lambda"],
            cwd=root,
            capture_output=True,
            text=True,
            check=True,
        ).stdout.split()
        return {
            name: (root / name).read_text(encoding="utf-8", errors="replace")
            for name in names
            if name.endswith(".py") and name != "gco_mcp/resources/config.py"
        }

    def test_every_listed_variable_is_read_somewhere(self):
        """GCO_CONFIG_PATH, DLQ_URL and DYNAMODB_TABLE were listed; nothing reads them."""
        import re

        sources = self._sources()
        unread = sorted(
            name
            for name in self._listed()
            if not any(re.search(rf"\b{name}\b", text) for text in sources.values())
        )
        assert unread == []

    def test_every_gco_variable_the_server_names_is_listed(self):
        """GCO_PROJECT_ROOT, GCO_MISSION_STATE_BACKEND and others were missing."""
        import re

        named = {
            name
            for path, text in self._sources().items()
            if path.startswith("gco_mcp/")
            for name in re.findall(r"[\"'](GCO_[A-Z0-9_]+)[\"']", text)
        }
        assert sorted(named - self._listed()) == []
        assert {"GCO_PROJECT_ROOT", "GCO_PROJECT_NAME", "GCO_DEFAULT_REGION"} <= self._listed()

    def test_aws_region_is_not_described_as_the_cli_default_region(self):
        content = (
            asyncio.run(run_mcp.mcp.read_resource("config://gco/env-vars")).contents[0].content
        )
        (row,) = [line for line in content.splitlines() if line.startswith("| `AWS_REGION`")]
        assert "not the CLI's default Region" in row
