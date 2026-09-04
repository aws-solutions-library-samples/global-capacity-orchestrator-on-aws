"""Residual argv, warning, search, and staging coverage for MCP tool wrappers."""

from __future__ import annotations

import asyncio
import json
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "gco_mcp"))

from tools import docs as docs_tool  # noqa: E402
from tools import examples as examples_tool  # noqa: E402

from tests.test_mcp_tool_wrapper_gap_coverage import (  # noqa: E402
    CLI_RESULT,
    FLAG_DESTRUCTIVE_OPERATIONS,
    FLAG_MODEL_UPLOAD,
    _assert_cli_call,
    _isolated_tool_module,
)
from tests.test_semantic_progress_tool import (  # noqa: E402
    call_judge,
    force_unregister_judge,
    import_judge_module,
)
from tests.test_storage_mcp import _load_storage_tool  # noqa: E402


@pytest.mark.parametrize(
    ("function_name", "args", "kwargs", "expected_argv"),
    [
        ("cost_workloads", (), {}, ("costs", "workloads")),
        (
            "cost_allocation_activate",
            (),
            {"extra_tags": ["Team"], "backfill_from": "2026-01-01"},
            (
                "costs",
                "allocation",
                "activate",
                "-y",
                "-t",
                "Team",
                "--backfill-from",
                "2026-01-01",
            ),
        ),
        ("cost_k8s_namespaces", (), {}, ("costs", "k8s", "namespaces", "--days", "7")),
        (
            "cost_k8s_trend",
            (),
            {},
            ("costs", "k8s", "trend", "--days", "14", "--granularity", "daily"),
        ),
        ("cost_report_status", (), {}, ("costs", "report", "status")),
        (
            "cost_report_list",
            (),
            {"region": "eu-west-1", "adhoc": False},
            ("costs", "report", "list", "-l", "20", "-r", "eu-west-1"),
        ),
        (
            "cost_report_generate",
            (),
            {},
            ("costs", "report", "generate", "--window-hours", "24"),
        ),
    ],
)
def test_cost_wrapper_optional_argv_matrix(
    function_name: str,
    args: tuple[Any, ...],
    kwargs: dict[str, Any],
    expected_argv: tuple[str, ...],
) -> None:
    """Omitted and supplied cost options produce exact literal CLI argv."""
    with _isolated_tool_module("costs") as loaded:
        _assert_cli_call(loaded, function_name, args, kwargs, expected_argv)


@pytest.mark.parametrize(
    "tool_name",
    ["analytics", "models", "nodepools", "queue", "templates", "webhooks"],
)
def test_warning_helpers_are_noops_without_request_context(tool_name: str) -> None:
    """Destructive warnings cannot fail a direct/non-MCP invocation."""
    with _isolated_tool_module(tool_name) as loaded:
        assert asyncio.run(loaded.module._ctx_warning("careful")) is None
        loaded.dependencies.get_context.assert_called_once_with()


@pytest.mark.parametrize(
    "tool_name",
    ["analytics", "models", "nodepools", "queue", "templates", "webhooks"],
)
def test_warning_helpers_swallow_client_dispatch_failure(tool_name: str) -> None:
    """A disconnected client cannot prevent the requested operation."""
    with _isolated_tool_module(tool_name) as loaded:
        context = MagicMock()
        context.warning = AsyncMock(side_effect=RuntimeError("transport closed"))
        loaded.dependencies.get_context.side_effect = None
        loaded.dependencies.get_context.return_value = context

        assert asyncio.run(loaded.module._ctx_warning("careful")) is None
        context.warning.assert_awaited_once_with("careful")


def test_images_delete_repo_warns_and_delegates_force() -> None:
    """Repository deletion carries an explicit warning and force value."""
    with _isolated_tool_module("images", FLAG_DESTRUCTIVE_OPERATIONS) as loaded:
        manager = MagicMock()
        manager.delete_repo.return_value = {"deleted": "gco/trainer"}
        loaded.module._get_manager = MagicMock(return_value=manager)
        loaded.module._ctx_warning = AsyncMock()

        result = asyncio.run(loaded.module.images_delete_repo("trainer", force=True))

        assert json.loads(result) == {"deleted": "gco/trainer"}
        loaded.module._ctx_warning.assert_awaited_once_with(
            "Deleting repository gco/trainer (force=True) — this cannot be undone."
        )
        manager.delete_repo.assert_called_once_with("trainer", force=True)


def test_docs_search_ignores_malformed_lists_but_scores_summary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Catalog type drift is ignored while valid summary text remains searchable."""
    monkeypatch.setattr(
        docs_tool,
        "_catalog",
        lambda: {
            "odd": {
                "topics": "capacity",
                "keywords": "gpu",
                "summary": "needle",
            }
        },
    )

    assert docs_tool._search(None, "capacity") == []
    assert docs_tool._search("needle", None) == [("odd", 1)]


def test_example_search_skips_category_mismatch_and_malformed_lists(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Example filtering remains deterministic for defensive metadata shapes."""
    monkeypatch.setattr(
        examples_tool,
        "EXAMPLE_METADATA",
        {
            "odd": {
                "category": "batch",
                "keywords": "not-a-list",
                "summary": "needle",
                "use_cases": "not-a-list",
            }
        },
    )

    assert examples_tool._search(None, "other", None, None) == []
    assert examples_tool._search("needle", None, None, None) == [("odd", 2)]


@pytest.mark.parametrize("failure_type", [OSError, ValueError])
def test_storage_upload_reports_resolver_failures_without_cli(
    failure_type: type[Exception],
) -> None:
    """Local upload confinement failures are structured before staging."""
    module, runner = _load_storage_tool(enabled=False, upload_enabled=True)
    module._resolve_upload_local_path = MagicMock(side_effect=failure_type("outside local root"))

    payload = json.loads(asyncio.run(module.upload_to_regional_bucket("../outside", "us-east-1")))

    assert payload == {
        "error": "outside local root",
        "code": "local_data_path_rejected",
    }
    assert runner.calls == []


def test_storage_inventory_builds_optional_region_argv() -> None:
    """S3 inventory omits or includes the region flag exactly once."""
    module, runner = _load_storage_tool(enabled=False)

    assert json.loads(asyncio.run(module.s3_inventory()))["args"] == [
        "storage",
        "s3-inventory",
    ]
    assert json.loads(asyncio.run(module.s3_inventory("eu-west-1")))["args"] == [
        "storage",
        "s3-inventory",
        "--region",
        "eu-west-1",
    ]
    assert runner.calls[-2:] == [
        ("storage", "s3-inventory"),
        ("storage", "s3-inventory", "--region", "eu-west-1"),
    ]


def test_storage_upload_worker_forwards_snapshot_descriptor() -> None:
    """The worker owns staging until the descriptor-backed CLI call returns."""
    module, runner = _load_storage_tool(enabled=False, upload_enabled=True)
    contract = object()
    module._resolve_upload_local_path = MagicMock(return_value=contract)

    @contextmanager
    def fake_stage(received: object) -> Iterator[SimpleNamespace]:
        assert received is contract
        yield SimpleNamespace(argument="snapshot/model.bin", directory_fd=7)

    module.stage_upload_path = fake_stage
    result = asyncio.run(module.upload_to_regional_bucket("model.bin", "us-east-1", "models"))

    assert json.loads(result)["args"] == [
        "models",
        "upload-regional",
        "snapshot/model.bin",
        "-r",
        "us-east-1",
        "--prefix",
        "models",
    ]
    assert runner.call_kwargs[-1]["pass_fds"] == (7,)


def test_model_upload_worker_forwards_snapshot_descriptor() -> None:
    """Model upload passes the private descriptor and exact name argv."""
    with _isolated_tool_module("models", FLAG_MODEL_UPLOAD) as loaded:
        contract = object()
        loaded.resolve_local_path.return_value = contract

        @contextmanager
        def fake_stage(received: object) -> Iterator[SimpleNamespace]:
            assert received is contract
            yield SimpleNamespace(argument="/dev/fd/9/weights", directory_fd=9)

        loaded.module.stage_upload_path = fake_stage
        result = asyncio.run(loaded.module.models_upload("llama", "weights"))

        assert result == CLI_RESULT
        loaded.run_cli.assert_called_once_with(
            "models",
            "upload",
            "/dev/fd/9/weights",
            "--name",
            "llama",
            pass_fds=(9,),
        )


def test_semantic_progress_wraps_unexpected_internal_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Nothing outside the documented judge envelope escapes the tool."""
    monkeypatch.setenv("GCO_ENABLE_SEMANTIC_PROGRESS", "true")
    module = import_judge_module()
    try:
        monkeypatch.setattr(
            module.judge_prompt,
            "build_prompt",
            MagicMock(side_effect=RuntimeError("unexpected failure")),
        )
        result = call_judge(module, directive="finish the objective")
    finally:
        force_unregister_judge()

    assert result == {
        "code": module.ErrorCode.SAMPLING_TRANSPORT_ERROR,
        "details": {"reason": "unexpected", "detail": "unexpected failure"},
    }
