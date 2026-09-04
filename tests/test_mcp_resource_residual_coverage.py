"""Residual behavior coverage for the non-Mission MCP resource registry."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
MCP_ROOT = PROJECT_ROOT / "gco_mcp"
sys.path.insert(0, str(MCP_ROOT))

import server  # noqa: E402
from resources import (  # noqa: E402
    _eks,
    ci,
    clients,
    cluster,
    config,
    demos,
    docs,
    iam_policies,
    images,
    infra,
    k8s,
    scripts,
)
from resources import self as self_resources  # noqa: E402
from resources import tasks as task_resources  # noqa: E402
from resources import tests as test_resources  # noqa: E402


def _patch_ci_roots(monkeypatch: pytest.MonkeyPatch, root: Path) -> None:
    monkeypatch.setattr(ci, "GITHUB_DIR", root)
    monkeypatch.setattr(ci, "GITHUB_WORKFLOWS_DIR", root / "workflows")
    monkeypatch.setattr(ci, "GITHUB_ACTIONS_DIR", root / "actions")
    monkeypatch.setattr(ci, "GITHUB_SCRIPTS_DIR", root / "scripts")
    monkeypatch.setattr(ci, "GITHUB_ISSUE_TEMPLATE_DIR", root / "ISSUE_TEMPLATE")
    monkeypatch.setattr(ci, "GITHUB_KIND_DIR", root / "kind")
    monkeypatch.setattr(ci, "GITHUB_CODEQL_DIR", root / "codeql")


def test_eks_rejects_overlong_region_before_aws_calls() -> None:
    """Bounded region validation prevents oversized context ARNs."""
    region = f"us-{'a' * 28}-1"
    assert len(region) > _eks._MAX_REGION_LENGTH
    assert _eks.is_valid_region(region) is False

    with (
        patch.object(_eks.boto3, "client") as client,
        pytest.raises(ValueError, match="invalid AWS region"),
    ):
        _eks.eks_context_for_region(region, "gco")

    client.assert_not_called()


@pytest.mark.parametrize("partition", [None, "", "AWS"])
def test_eks_rejects_invalid_sdk_partition(partition: object) -> None:
    """SDK partition output is validated before interpolation into an ARN."""
    sts = MagicMock()
    sts.get_caller_identity.return_value = {"Account": "123456789012"}
    session = MagicMock()
    session.get_partition_for_region.return_value = partition
    with (
        patch.object(_eks.boto3, "client", return_value=sts),
        patch.object(_eks.boto3.session, "Session", return_value=session),
        pytest.raises(ValueError, match="invalid partition"),
    ):
        _eks.eks_context_for_region("us-east-1", "gco")


def test_ci_reader_rejects_unsupported_existing_file_and_shortens_missing_error(
    tmp_path: Path,
) -> None:
    """CI resources enforce extensions and handle an unavailable listing root."""
    unsupported = tmp_path / "secret.bin"
    unsupported.write_bytes(b"secret")
    assert ci._ci_read(unsupported, "Workflow", tmp_path).startswith("File type '.bin'")
    assert ci._ci_read(tmp_path / "missing.yml", "Workflow", tmp_path / "absent") == (
        "Workflow 'missing.yml' not found."
    )


def test_ci_index_handles_absent_and_empty_optional_trees(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Optional CI sections appear only when eligible artifacts exist."""
    root = tmp_path / ".github"
    root.mkdir()
    _patch_ci_roots(monkeypatch, root)

    absent = ci.ci_index()
    assert ci.ci_action_resource("missing") == "Composite action 'missing' not found."
    assert ci.ci_template_resource("missing.md") == "Template 'missing.md' not found. Available:\n"
    assert "## Workflows" not in absent
    assert "## Composite Actions" not in absent
    assert "## Scripts" not in absent
    assert "## CodeQL Configuration" not in absent
    assert "## Kind Cluster Configuration" not in absent
    assert "## Repo Automation" not in absent
    assert "## Related Resources" in absent

    for child in ("workflows", "actions", "scripts", "codeql", "kind"):
        (root / child).mkdir()
    templates = root / "ISSUE_TEMPLATE"
    templates.mkdir()
    (templates / "00-ignore.txt").write_text("ignored", encoding="utf-8")
    (templates / "zz-valid.md").write_text("valid", encoding="utf-8")

    empty = ci.ci_index()
    assert "## Workflows" not in empty
    assert "## Composite Actions" not in empty
    assert "## Scripts" not in empty
    assert "## CodeQL Configuration" not in empty
    assert "## Kind Cluster Configuration" not in empty
    assert "ci://gco/templates/zz-valid.md" in empty
    assert "00-ignore.txt" not in empty


def test_ci_action_template_and_config_fallbacks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Action YAML fallback, issue templates, and missing configs are deterministic."""
    root = tmp_path / ".github"
    root.mkdir()
    _patch_ci_roots(monkeypatch, root)
    action = root / "actions" / "demo"
    action.mkdir(parents=True)
    (action / "action.yaml").write_text("name: demo\n", encoding="utf-8")
    templates = root / "ISSUE_TEMPLATE"
    templates.mkdir()
    (templates / "bug.md").write_text("bug body\n", encoding="utf-8")

    assert ci.ci_action_resource("demo") == "name: demo\n"
    assert ci.ci_action_resource("missing").startswith("Composite action 'missing' not found")
    assert ci.ci_template_resource("bug.md") == "bug body\n"
    assert (
        ci.ci_template_resource("missing.md")
        == "Template 'missing.md' not found. Available:\nbug.md"
    )
    assert ci.ci_config_resource("CODEOWNERS") == "Config file 'CODEOWNERS' not found."


def test_clients_reject_existing_unsupported_example(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    example = tmp_path / "payload.txt"
    example.write_text("secret", encoding="utf-8")
    monkeypatch.setattr(clients, "CLIENT_EXAMPLES_DIR", tmp_path)
    assert clients.client_example_resource("payload.txt") == "File type '.txt' not served."


def test_cluster_pending_pods_reports_context_resolution_failure() -> None:
    """Credential errors fail before any kubectl subprocess is created."""
    with (
        patch.object(cluster, "eks_context_for_region", side_effect=RuntimeError("no credentials")),
        patch.object(cluster.cli_runner.subprocess, "run") as run,
    ):
        result = cluster._pending_pods("us-east-1")

    assert result == {
        "error": "unable to resolve EKS context",
        "detail": "no credentials",
    }
    run.assert_not_called()


def test_config_resource_reports_missing_cdk_json(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(config, "PROJECT_ROOT", tmp_path)
    assert config.cdk_json_resource() == "cdk.json not found."


def test_demos_index_and_reader_handle_empty_layout_and_bad_extension(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(demos, "DEMO_DIR", tmp_path)
    index = demos.demos_index()
    assert "demos://gco/walkthrough/" not in index
    assert "demos://gco/script/" not in index

    (tmp_path / "bad.txt").write_text("not served", encoding="utf-8")
    assert demos.demo_resource("bad.txt").startswith("File type '.txt' not served")


def test_root_doc_resources_cover_missing_and_metadata_free_tenets(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(docs, "PROJECT_ROOT", tmp_path)
    monkeypatch.setitem(
        docs.ROOT_DOC_METADATA,
        "TENETS",
        {"path": "TENETS.md", "topics": [], "related": []},
    )

    assert docs.quickstart_resource() == "QUICKSTART.md not found."
    assert docs.contributing_resource() == "CONTRIBUTING.md not found."
    assert docs.tenets_resource() == "TENETS.md not found."

    (tmp_path / "TENETS.md").write_text("# Tenets\n", encoding="utf-8")
    assert docs.tenets_resource() == "\n\n# Tenets\n"


def test_doc_and_package_resources_cover_plain_header_and_missing_file_shapes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    docs_dir = tmp_path / "docs"
    docs_dir.mkdir()
    (docs_dir / "PLAIN.md").write_text("plain body\n", encoding="utf-8")
    (docs_dir / "RICH.md").write_text("rich body\n", encoding="utf-8")
    package = tmp_path / "package.md"
    package.write_text("package body\n", encoding="utf-8")
    monkeypatch.setattr(docs, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(docs, "DOCS_DIR", docs_dir)
    monkeypatch.setattr(
        docs,
        "DOC_METADATA",
        {
            "PLAIN": {"topics": "bad", "related": "bad"},
            "RICH": {"topics": ["runtime"], "related": ["PLAIN"]},
        },
    )
    monkeypatch.setattr(
        docs,
        "PACKAGE_DOC_METADATA",
        {
            "missing": {"path": "missing.md", "topics": [], "related": []},
            "plain": {"path": "package.md", "topics": [], "related": []},
            "rich": {"path": "package.md", "topics": ["mcp"], "related": ["PLAIN"]},
        },
    )

    assert docs.doc_resource("PLAIN") == "plain body\n"
    assert docs.doc_resource("RICH").startswith("<!-- Topics: runtime -->\n<!-- Related: PLAIN -->")
    assert docs.package_doc_resource("missing") == (
        "Package doc 'missing' file not found at 'missing.md'."
    )
    assert docs.package_doc_resource("plain") == "package body\n"
    assert docs.package_doc_resource("rich").startswith(
        "<!-- Topics: mcp -->\n<!-- Related: PLAIN -->"
    )


def test_example_resources_cover_missing_readme_orphan_and_full_metadata(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    examples = tmp_path / "examples"
    examples.mkdir()
    (examples / "orphan.yaml").write_text("kind: Job\n", encoding="utf-8")
    (examples / "plain.yaml").write_text("kind: Service\n", encoding="utf-8")
    (examples / "rich.yaml").write_text("kind: Pod\n", encoding="utf-8")
    monkeypatch.setattr(docs, "EXAMPLES_DIR", examples)
    monkeypatch.setattr(
        docs,
        "EXAMPLE_METADATA",
        {
            "plain": {
                "category": "Service",
                "summary": "plain example",
                "keywords": [],
                "instance_types": [],
                "use_cases": [],
                "related": [],
            },
            "rich": {
                "category": "Jobs",
                "summary": "rich example",
                "gpu": "NVIDIA",
                "opt_in": "GCO_ENABLE_RICH",
                "submission": "gco jobs submit rich.yaml",
                "keywords": ["gpu"],
                "instance_types": ["g6.xlarge"],
                "use_cases": ["training"],
                "related": ["orphan"],
            },
        },
    )

    assert docs.examples_readme_resource() == "Examples README.md not found."
    assert docs.example_resource("orphan") == "kind: Job\n"
    plain = docs.example_resource("plain")
    assert "# Category: Service" in plain
    assert "# Keywords:" not in plain
    assert "# Instance Types:" not in plain
    assert "# Use Cases:" not in plain
    rich = docs.example_resource("rich")
    for expected in (
        "# Keywords: gpu",
        "# Instance Types: g6.xlarge",
        "# Use Cases: training",
        "# Related: orphan",
        "# --- Manifest begins below ---",
    ):
        assert expected in rich


def test_related_docs_and_adr_catalog_handle_no_relations_or_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        docs,
        "DOC_METADATA",
        {
            "target": {"summary": "target", "related": "not-a-list"},
            "other": {"summary": "other", "related": "not-a-list"},
        },
    )
    monkeypatch.setattr(docs, "ADR_DIR", tmp_path / "missing")

    assert docs.docs_by_related_resource("target") == "# Docs related to target\n"
    assert docs._adr_record_files() == []


def test_iam_policy_index_without_readme_and_successful_policy_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    policy = tmp_path / "policy.json"
    policy.write_text('{"Version":"2012-10-17"}\n', encoding="utf-8")
    monkeypatch.setattr(iam_policies, "IAM_POLICIES_DIR", tmp_path)

    index = iam_policies.iam_policies_index()
    assert "iam://gco/policies/policy.json" in index
    assert "iam://gco/policies/README" not in index
    assert iam_policies.iam_policy_resource("policy.json") == '{"Version":"2012-10-17"}\n'


@pytest.mark.parametrize(
    ("method", "call", "expected"),
    [
        ("list_repos", lambda: images.images_index(), "Failed to list repositories: boom"),
        ("list_tags", lambda: images.images_tags_resource("trainer"), "Failed to list tags: boom"),
        (
            "replication_status",
            lambda: images.images_replication_status_resource(),
            "Failed to read replication state: boom",
        ),
    ],
)
def test_image_resources_render_manager_failures(method: str, call: object, expected: str) -> None:
    manager = MagicMock()
    getattr(manager, method).side_effect = RuntimeError("boom")
    with patch.object(images, "_get_manager", return_value=manager):
        result = call()  # type: ignore[operator]
    assert expected in result


def test_image_describe_failure_is_structured_json() -> None:
    manager = MagicMock()
    manager.describe.side_effect = RuntimeError("denied")
    with patch.object(images, "_get_manager", return_value=manager):
        payload = json.loads(images.images_describe_resource("trainer", "v1"))
    assert payload == {"error": "denied", "name": "gco/trainer", "tag": "v1"}


def test_image_indexes_skip_non_gco_and_unaddressable_duplicate_tags() -> None:
    manager = MagicMock()
    manager.list_repos.return_value = [
        {"name": "other/repo"},
        {"name": "gco/trainer"},
    ]
    manager.list_tags.return_value = [
        {"tag": None, "digest": "sha:none"},
        {"tag": "v1", "digest": "sha:one"},
        {"tag": "v1", "digest": "sha:duplicate"},
        {"tag": "v2", "digest": "sha:two"},
    ]
    with patch.object(images, "_get_manager", return_value=manager):
        index = images.images_index()
        tags = images.images_tags_resource("trainer")

    assert "images://gco/trainer/tags" in index
    assert "images://gco/other/repo/tags" not in index
    assert "(untagged)" in tags
    assert "/None" not in tags
    assert tags.count("images://gco/trainer/v1") == 1
    assert tags.count("images://gco/trainer/v2") == 1


def test_infra_index_and_helm_resource_cover_optional_absence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    dockerfiles = tmp_path / "dockerfiles"
    dockerfiles.mkdir()
    (dockerfiles / "worker.Dockerfile").write_text("FROM scratch\n", encoding="utf-8")
    monkeypatch.setattr(infra, "DOCKERFILES_DIR", dockerfiles)
    monkeypatch.setattr(infra, "HELM_CHARTS_FILE", tmp_path / "missing-charts.yaml")

    index = infra.infra_index()
    assert "worker.Dockerfile" in index
    assert "dockerfiles/README" not in index
    assert "helm/charts" not in index
    assert infra.helm_charts_resource() == "charts.yaml not found."


def test_k8s_index_and_live_validation_cover_readme_and_context_failures(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "10-app.yaml").write_text("apiVersion: v1\n", encoding="utf-8")
    monkeypatch.setattr(k8s, "MANIFESTS_DIR", tmp_path)
    index = k8s.k8s_manifests_index()
    assert "10-app.yaml" in index
    assert "README.md" not in index

    with (
        patch.object(k8s, "eks_context_for_region") as resolve_context,
        patch.object(k8s.cli_runner.subprocess, "run") as run,
    ):
        assert json.loads(
            k8s._k8s_live_resource_for_region("Bad-Region", "default", "Pod", "app")
        ) == {"error": "invalid region", "value": "Bad-Region"}
        resolve_context.assert_not_called()
        run.assert_not_called()

    with (
        patch.object(k8s, "eks_context_for_region", side_effect=RuntimeError("no credentials")),
        patch.object(k8s.cli_runner.subprocess, "run") as run,
    ):
        payload = json.loads(
            k8s._k8s_live_resource_for_region("us-east-1", "default", "Pod", "app")
        )
    assert payload == {"error": "unable to resolve EKS context", "detail": "no credentials"}
    run.assert_not_called()


def test_scripts_index_without_readme_and_bad_extension(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "check.py").write_text("pass\n", encoding="utf-8")
    (tmp_path / "secret.bin").write_bytes(b"secret")
    monkeypatch.setattr(scripts, "SCRIPTS_DIR", tmp_path)

    index = scripts.scripts_index()
    assert "scripts://gco/check.py" in index
    assert "scripts://gco/README" not in index
    assert scripts.script_resource("secret.bin") == "File type '.bin' not served."


def test_self_source_info_handles_unwrap_source_and_line_fallbacks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Registry source metadata survives wrappers, loaders, and external callables."""

    def local_function() -> None:
        return None

    monkeypatch.setattr(
        self_resources.inspect, "unwrap", MagicMock(side_effect=RuntimeError("cycle"))
    )
    relative_path, line = self_resources._source_info_for_fn(local_function)
    assert relative_path == "tests/test_mcp_resource_residual_coverage.py"
    assert line == local_function.__code__.co_firstlineno

    monkeypatch.setattr(self_resources.inspect, "unwrap", lambda fn: fn)
    monkeypatch.setattr(
        self_resources.inspect,
        "getsourcefile",
        MagicMock(side_effect=OSError("source unavailable")),
    )
    assert self_resources._source_info_for_fn(local_function) == (
        None,
        local_function.__code__.co_firstlineno,
    )


def test_self_source_info_uses_getsourcelines_and_preserves_external_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class CallableWithoutCode:
        def __call__(self) -> None:
            return None

    target = CallableWithoutCode()
    monkeypatch.setattr(self_resources.inspect, "unwrap", lambda fn: fn)
    monkeypatch.setattr(
        self_resources.inspect,
        "getsourcefile",
        lambda _fn: "/opt/site-packages/plugin.py",
    )
    monkeypatch.setattr(self_resources.inspect, "getsourcelines", lambda _fn: (["x"], 77))

    assert self_resources._source_info_for_fn(target) == ("/opt/site-packages/plugin.py", 77)

    monkeypatch.setattr(
        self_resources.inspect,
        "getsourcelines",
        MagicMock(side_effect=TypeError("built in")),
    )
    assert self_resources._source_info_for_fn(target) == ("/opt/site-packages/plugin.py", None)


@pytest.mark.asyncio
async def test_self_registry_snapshots_degrade_each_backend_independently() -> None:
    """Transient registry failures produce partial, never fatal, introspection."""
    with patch.object(server.mcp, "_list_tools", new=AsyncMock(side_effect=RuntimeError("tools"))):
        assert await self_resources._list_tools_async() == []

    resource = SimpleNamespace(uri="test://one")
    template = SimpleNamespace(uri_template="test://{name}")
    with (
        patch.object(server.mcp, "_list_resources", new=AsyncMock(side_effect=RuntimeError("r"))),
        patch.object(
            server.mcp, "_list_resource_templates", new=AsyncMock(return_value=[template])
        ),
    ):
        assert await self_resources._list_resources_async() == ([], [template])
    with (
        patch.object(server.mcp, "_list_resources", new=AsyncMock(return_value=[resource])),
        patch.object(
            server.mcp,
            "_list_resource_templates",
            new=AsyncMock(side_effect=RuntimeError("t")),
        ),
    ):
        assert await self_resources._list_resources_async() == ([resource], [])


@pytest.mark.asyncio
async def test_self_registry_contracts_are_explicit_and_fail_loudly(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Not-found typing and feature-table drift are executable contracts."""
    from fastmcp.exceptions import NotFoundError

    assert isinstance(self_resources._make_not_found("missing"), NotFoundError)
    monkeypatch.setitem(self_resources._TOOL_GATING_TABLE, "broken-tool", "UNKNOWN_FLAG")
    with pytest.raises(KeyError, match="UNKNOWN_FLAG"):
        await self_resources._feature_flags()


@pytest.mark.asyncio
async def test_live_resource_registry_has_unique_uris_and_source_metadata() -> None:
    """The live registry exposes unique resources with inspectable source locations."""
    payload = json.loads(await self_resources._resources_index())
    resource_uris = [entry["uri"] for entry in payload["resources"]]
    template_uris = [entry["uri_template"] for entry in payload["templates"]]
    assert len(resource_uris) == len(set(resource_uris))
    assert len(template_uris) == len(set(template_uris))

    source_paths = {
        entry["source_path"]
        for entry in [*payload["resources"], *payload["templates"]]
        if entry["source_path"]
    }
    assert {
        "gco_mcp/resources/ci.py",
        "gco_mcp/resources/docs.py",
        "gco_mcp/resources/images.py",
        "gco_mcp/resources/k8s.py",
        "gco_mcp/resources/self.py",
        "gco_mcp/resources/source.py",
        "gco_mcp/resources/tasks.py",
    } <= source_paths


def test_task_record_dict_fast_path_preserves_identity() -> None:
    record = {"status": "working"}
    assert task_resources._coerce_to_dict(record) is record


def test_test_resource_index_handles_empty_and_partial_bats_layouts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(test_resources, "TESTS_DIR", tmp_path)
    empty = test_resources.tests_index()
    assert "## Test Infrastructure" not in empty
    assert "## Test Files" not in empty
    assert "## BATS Shell Tests" not in empty

    bats = tmp_path / "BATS"
    bats.mkdir()
    assert "## BATS Shell Tests" not in test_resources.tests_index()
    (bats / "smoke.bats").write_text("@test smoke {}\n", encoding="utf-8")
    partial = test_resources.tests_index()
    assert "## BATS Shell Tests" in partial
    assert "tests://gco/BATS/smoke.bats" in partial
    assert "tests://gco/BATS/README.md" not in partial


def test_test_resource_rejects_existing_unsupported_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "secret.txt").write_text("secret", encoding="utf-8")
    monkeypatch.setattr(test_resources, "TESTS_DIR", tmp_path)
    assert test_resources.test_file_resource("secret.txt") == "File type '.txt' not served."
