"""
Tests for the new MCP resource groups added during the refactor:
- tests:// — Test suite documentation and patterns
- config:// — CDK configuration, feature toggles, environment variables
- docs://gco/examples/guide — Example manifest creation guide
- Enhanced example metadata in docs://gco/examples/{name}
"""

import asyncio
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
        assert "config://gco/feature-toggles" in content
        assert "config://gco/env-vars" in content

    def test_config_cdk_json_returns_content(self):
        result = asyncio.run(run_mcp.mcp.read_resource("config://gco/cdk.json"))
        content = result.contents[0].content
        assert len(content) > 100
        # cdk.json should contain JSON
        assert "{" in content

    def test_config_feature_toggles_returns_content(self):
        result = asyncio.run(run_mcp.mcp.read_resource("config://gco/feature-toggles"))
        content = result.contents[0].content
        assert "Feature Toggles" in content
        assert len(content) > 50

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
        """All tool modules should be importable."""
        from tools import capacity, costs, inference, jobs, models, stacks, storage  # noqa: F401

    def test_resources_are_importable(self):
        """All resource modules should be importable."""
        from resources import (  # noqa: F401
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

    def test_audit_module_importable(self):
        from audit import _sanitize_arguments, audit_logged  # noqa: F401

    def test_cli_runner_importable(self):
        from cli_runner import _run_cli  # noqa: F401

    def test_version_module_importable(self):
        from version import get_project_version  # noqa: F401

        assert get_project_version() != "unknown"
