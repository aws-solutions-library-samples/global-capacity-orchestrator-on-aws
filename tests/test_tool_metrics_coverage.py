"""Coverage-focused tests for the metric-reader MCP tool wrappers.

These tests target the validation guards, error envelopes, and read-helper
branches of ``gco_mcp/tools/metrics.py`` that the success-path suite in
``tests/test_metric_readers_tools.py`` does not reach: the CloudWatch
window-parse branch and its failure envelopes, the job-log
extraction/regex/aggregation guards and retrieval-failure paths, the
shared-storage read helper, the local-file read helper, and the flag-gated
local-file reader tool (exercised by re-importing the module with the gate on).

They *add to* — never replace — the sibling success-path suite, and follow the
same import convention: ``gco_mcp/`` is placed on ``sys.path`` and ``run_mcp``
is imported first so its tool-registration side effect runs before
``tools.metrics`` is used.
"""

from __future__ import annotations

import asyncio
import contextlib
import importlib
import json
import sys
from datetime import datetime
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# ``gco_mcp/run_mcp.py`` puts ``gco_mcp/`` on ``sys.path`` at runtime; mirror that
# here so ``tools.metrics`` (and the pure ``metric_readers`` package beside it)
# import the same way they do in production, matching the sibling tool tests.
sys.path.insert(0, str(Path(__file__).parent.parent / "gco_mcp"))

from metric_readers.shape import ErrorCode, MetricReaderError  # noqa: E402

_LOCAL_FILE_TOOL = "metrics_from_local_file"


def _import_metrics_tool_module():
    """Import ``tools.metrics`` or skip when its FastMCP/server deps are absent.

    Mirrors the guard used by the sibling tool tests: the pure reader package
    needs only the standard library, but the tool wrappers pull in ``server``
    (FastMCP) and ``audit``, so a minimal environment skips rather than fails.
    ``run_mcp`` is imported first for its tool-registration side effect.
    """
    try:
        import run_mcp  # noqa: F401 - import-time side effect registers the tools
        import tools.metrics as metrics_module
    except Exception as exc:  # noqa: BLE001 - any import-surface failure -> skip
        pytest.skip(f"tools.metrics not importable in this environment: {exc}")
    return metrics_module


# ===========================================================================
# CloudWatch reader - window-parse branch and failure envelopes
# ===========================================================================


def _cloudwatch_client_returning(datapoints: list[dict]) -> MagicMock:
    """A mock boto3 CloudWatch client whose ``get_metric_statistics`` returns ``datapoints``."""
    client = MagicMock()
    client.get_metric_statistics.return_value = {"Datapoints": datapoints}
    return client


def test_cloudwatch_explicit_window_is_parsed_and_returns_canonical_shape() -> None:
    """Explicit ISO ``start_time``/``end_time`` are parsed and drive a successful read."""
    metrics_module = _import_metrics_tool_module()
    client = _cloudwatch_client_returning(
        [{"Timestamp": datetime(2024, 3, 1, 0, 0, 0), "Average": 0.5}]
    )
    with patch("boto3.client", return_value=client):
        result = asyncio.run(
            metrics_module.metrics_cloudwatch_get(
                metric_name="GpuUtilization",
                namespace="GCO/Training",
                region="us-east-1",
                statistic="Average",
                start_time="2024-03-01T00:00:00",
                end_time="2024-03-01T01:00:00",
            )
        )
    assert "code" not in result
    assert result["metrics"] == {"GpuUtilization": 0.5}


def test_cloudwatch_reader_error_returns_envelope() -> None:
    """A reader-raised MetricReaderError (no datapoints) becomes its error envelope."""
    metrics_module = _import_metrics_tool_module()
    client = _cloudwatch_client_returning([])
    with patch("boto3.client", return_value=client):
        result = asyncio.run(
            metrics_module.metrics_cloudwatch_get(
                metric_name="GpuUtilization",
                namespace="GCO/Training",
                region="us-east-1",
            )
        )
    assert result["code"] == ErrorCode.NO_DATAPOINTS
    assert "metrics" not in result


def test_cloudwatch_invalid_window_maps_to_aws_unreachable() -> None:
    """An unparseable ISO window raises inside the body and maps to the unreachable catch-all."""
    metrics_module = _import_metrics_tool_module()
    result = asyncio.run(
        metrics_module.metrics_cloudwatch_get(
            metric_name="GpuUtilization",
            namespace="GCO/Training",
            region="us-east-1",
            start_time="not-a-timestamp",
            end_time="also-not-a-timestamp",
        )
    )
    assert result["code"] == ErrorCode.AWS_UNREACHABLE
    assert result["details"]["kind"] == "client_error"
    assert result["details"]["region"] == "us-east-1"
    assert "metrics" not in result


# ===========================================================================
# Job-log reader - extraction/aggregation guards and retrieval failures
# ===========================================================================


def _patch_job_logs(metrics_module, monkeypatch: pytest.MonkeyPatch, raw: str) -> None:
    """Patch the job-log retrieval seam to return ``raw`` as the CLI payload."""

    def _fake_run_cli(*_args: object, **_kwargs: object) -> str:
        return raw

    monkeypatch.setattr(metrics_module.cli_runner, "_run_cli", _fake_run_cli)


def test_job_logs_no_extraction_mode_is_invalid_extraction_mode() -> None:
    """Supplying neither a JSON key nor a regex is an invalid-extraction-mode envelope."""
    metrics_module = _import_metrics_tool_module()
    result = asyncio.run(metrics_module.metrics_from_job_logs(job_name="j", region="us-east-1"))
    assert result["code"] == ErrorCode.INVALID_EXTRACTION_MODE


def test_job_logs_both_extraction_modes_is_invalid_extraction_mode() -> None:
    """Supplying both a JSON key and a regex is an invalid-extraction-mode envelope."""
    metrics_module = _import_metrics_tool_module()
    result = asyncio.run(
        metrics_module.metrics_from_job_logs(
            job_name="j", region="us-east-1", json_key="loss", regex=r"loss=(\d+)"
        )
    )
    assert result["code"] == ErrorCode.INVALID_EXTRACTION_MODE


def test_job_logs_invalid_aggregation_is_invalid_aggregation_mode() -> None:
    """An unknown aggregation mode is rejected before any retrieval."""
    metrics_module = _import_metrics_tool_module()
    result = asyncio.run(
        metrics_module.metrics_from_job_logs(
            job_name="j", region="us-east-1", json_key="loss", aggregation="median"
        )
    )
    assert result["code"] == ErrorCode.INVALID_AGGREGATION_MODE


def test_job_logs_uncompilable_regex_is_invalid_regex() -> None:
    """A regex that does not compile is an invalid-regex envelope."""
    metrics_module = _import_metrics_tool_module()
    result = asyncio.run(
        metrics_module.metrics_from_job_logs(job_name="j", region="us-east-1", regex=r"loss=(\d+")
    )
    assert result["code"] == ErrorCode.INVALID_REGEX


def test_job_logs_regex_without_capture_group_is_invalid_regex() -> None:
    """A valid regex with no capture group is an invalid-regex envelope."""
    metrics_module = _import_metrics_tool_module()
    result = asyncio.run(
        metrics_module.metrics_from_job_logs(job_name="j", region="us-east-1", regex=r"loss=\d+")
    )
    assert result["code"] == ErrorCode.INVALID_REGEX
    assert (result["details"] or {}).get("reason") == "no capture group"


def test_job_logs_cli_error_payload_unknown_job(monkeypatch: pytest.MonkeyPatch) -> None:
    """A CLI error payload mentioning 'not found' is a log-retrieval failure (unknown_job)."""
    metrics_module = _import_metrics_tool_module()
    _patch_job_logs(metrics_module, monkeypatch, json.dumps({"error": "job not found"}))
    result = asyncio.run(
        metrics_module.metrics_from_job_logs(
            job_name="sft-run", region="us-east-1", json_key="loss"
        )
    )
    assert result["code"] == ErrorCode.LOG_RETRIEVAL_FAILED
    assert result["details"]["kind"] == "unknown_job"


def test_job_logs_cli_error_payload_unreachable(monkeypatch: pytest.MonkeyPatch) -> None:
    """A CLI error payload without 'not found' is a log-retrieval failure (unreachable)."""
    metrics_module = _import_metrics_tool_module()
    _patch_job_logs(metrics_module, monkeypatch, json.dumps({"error": "connection refused"}))
    result = asyncio.run(
        metrics_module.metrics_from_job_logs(
            job_name="sft-run", region="us-east-1", json_key="loss"
        )
    )
    assert result["code"] == ErrorCode.LOG_RETRIEVAL_FAILED
    assert result["details"]["kind"] == "unreachable"


def test_job_logs_no_candidates_is_no_match(monkeypatch: pytest.MonkeyPatch) -> None:
    """Logs that never carry the JSON key yield no candidates -> a no-match envelope."""
    metrics_module = _import_metrics_tool_module()
    _patch_job_logs(metrics_module, monkeypatch, "starting\nrunning\ndone\n")
    result = asyncio.run(
        metrics_module.metrics_from_job_logs(
            job_name="sft-run", region="us-east-1", json_key="loss"
        )
    )
    assert result["code"] == ErrorCode.NO_MATCH
    assert result["details"]["mode"] == "json_key"


def test_job_logs_generic_exception_maps_to_unreachable(monkeypatch: pytest.MonkeyPatch) -> None:
    """A non-reader exception during retrieval maps to the unreachable catch-all."""
    metrics_module = _import_metrics_tool_module()

    def _boom(*_args: object, **_kwargs: object) -> str:
        raise RuntimeError("kubectl exploded")

    monkeypatch.setattr(metrics_module.cli_runner, "_run_cli", _boom)
    result = asyncio.run(
        metrics_module.metrics_from_job_logs(
            job_name="sft-run", region="us-east-1", json_key="loss"
        )
    )
    assert result["code"] == ErrorCode.LOG_RETRIEVAL_FAILED
    assert result["details"]["kind"] == "unreachable"


# ===========================================================================
# Shared-storage read helper - payload / presence / happy read
# ===========================================================================


def test_read_shared_storage_cli_error_payload_is_file_not_found(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A CLI error payload means the artifact could not be read -> file-not-found."""
    metrics_module = _import_metrics_tool_module()

    def _fake_run_cli(*_args: object, **_kwargs: object) -> str:
        return json.dumps({"error": "NoSuchKey"})

    monkeypatch.setattr(metrics_module.cli_runner, "_run_cli", _fake_run_cli)
    with pytest.raises(MetricReaderError) as excinfo:
        metrics_module._read_shared_storage("runs/x.json", "us-east-1", max_bytes=1024)
    assert excinfo.value.code == ErrorCode.FILE_NOT_FOUND
    assert (excinfo.value.details or {}).get("path") == "runs/x.json"


def test_read_shared_storage_missing_file_is_file_not_found(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A clean CLI payload that wrote no file is a file-not-found error."""
    metrics_module = _import_metrics_tool_module()

    def _fake_run_cli(*_args: object, **_kwargs: object) -> str:
        # Valid JSON, no "error", but the download wrote no file.
        return "{}"

    monkeypatch.setattr(metrics_module.cli_runner, "_run_cli", _fake_run_cli)
    with pytest.raises(MetricReaderError) as excinfo:
        metrics_module._read_shared_storage("runs/x.json", "us-east-1", max_bytes=1024)
    assert excinfo.value.code == ErrorCode.FILE_NOT_FOUND


def test_read_shared_storage_reads_small_file(monkeypatch: pytest.MonkeyPatch) -> None:
    """A small downloaded file is read back verbatim (non-JSON CLI output is used as-is)."""
    metrics_module = _import_metrics_tool_module()

    def _fake_run_cli(*args: object, **_kwargs: object) -> str:
        # ``files download <path> <local_path> -r <region>`` - write a small
        # payload to the requested local destination; plain text return value
        # is not JSON, so the payload-error branch is skipped.
        local_path = args[3]
        with open(str(local_path), "wb") as handle:
            handle.write(b"hello metrics")
        return "downloaded ok"

    monkeypatch.setattr(metrics_module.cli_runner, "_run_cli", _fake_run_cli)
    content = metrics_module._read_shared_storage("runs/x.txt", "us-east-1", max_bytes=1024)
    assert content == b"hello metrics"


# ===========================================================================
# Shared-storage tool - aggregation guard and unexpected-error envelope
# ===========================================================================


def test_shared_storage_invalid_aggregation_is_invalid_aggregation_mode() -> None:
    """An unknown aggregation mode is rejected before any storage read."""
    metrics_module = _import_metrics_tool_module()
    result = asyncio.run(
        metrics_module.metrics_from_shared_storage_file(
            path="runs/m.json",
            region="us-east-1",
            field="loss",
            format="json",
            aggregation="median",
        )
    )
    assert result["code"] == ErrorCode.INVALID_AGGREGATION_MODE


def test_shared_storage_unexpected_error_maps_to_file_not_found(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A non-reader exception inside the body maps to the unreadable catch-all."""
    metrics_module = _import_metrics_tool_module()

    def _boom(_path: str, _region: str, _max_bytes: int) -> bytes:
        raise RuntimeError("storage backend exploded")

    monkeypatch.setattr(metrics_module, "_read_shared_storage", _boom)
    result = asyncio.run(
        metrics_module.metrics_from_shared_storage_file(
            path="runs/m.json", region="us-east-1", field="loss", format="json"
        )
    )
    assert result["code"] == ErrorCode.FILE_NOT_FOUND
    assert result["details"]["path"] == "runs/m.json"
    assert "metrics" not in result


# ===========================================================================
# Local-file read helper (module-scope, gate-independent)
# ===========================================================================


def test_read_local_file_missing_is_file_not_found(tmp_path: Path) -> None:
    """A path that is not a file is a file-not-found error."""
    metrics_module = _import_metrics_tool_module()
    missing = tmp_path / "nope.json"
    with pytest.raises(MetricReaderError) as excinfo:
        metrics_module._read_local_file(missing, "nope.json", max_bytes=1024)
    assert excinfo.value.code == ErrorCode.FILE_NOT_FOUND


def test_read_local_file_oversize_is_file_too_large(tmp_path: Path) -> None:
    """A file larger than the cap is a file-too-large error with size provenance."""
    metrics_module = _import_metrics_tool_module()
    artifact = tmp_path / "big.bin"
    artifact.write_bytes(b"x" * 4096)
    with pytest.raises(MetricReaderError) as excinfo:
        metrics_module._read_local_file(artifact, "big.bin", max_bytes=1024)
    assert excinfo.value.code == ErrorCode.FILE_TOO_LARGE
    details = excinfo.value.details or {}
    assert details.get("size") == 4096
    assert details.get("max_bytes") == 1024


def test_read_local_file_reads_small_file(tmp_path: Path) -> None:
    """A file within the cap is read back verbatim."""
    metrics_module = _import_metrics_tool_module()
    artifact = tmp_path / "small.json"
    artifact.write_bytes(b'{"loss": 1}')
    content = metrics_module._read_local_file(artifact, "small.json", max_bytes=1024)
    assert content == b'{"loss": 1}'


# ===========================================================================
# Flag-gated local-file reader tool (re-imported with the gate enabled)
# ===========================================================================


@contextlib.contextmanager
def _local_metrics_enabled(monkeypatch: pytest.MonkeyPatch, root: Path):
    """Re-import ``tools.metrics`` with the local-file gate enabled, yielding the module.

    Sets ``GCO_ENABLE_LOCAL_METRICS=true`` and ``GCO_METRICS_LOCAL_ROOT`` via
    monkeypatch (auto-restored), drops the cached ``tools.metrics`` so the
    module-level gate re-evaluates on re-import, and on exit force-unregisters
    the gated tool from the shared FastMCP singleton and restores the original
    module object - so no gated registration leaks into a sibling test.
    """
    monkeypatch.setenv("GCO_ENABLE_LOCAL_METRICS", "true")
    monkeypatch.setenv("GCO_METRICS_LOCAL_ROOT", str(root))
    original = sys.modules.get("tools.metrics")
    sys.modules.pop("tools.metrics", None)
    try:
        module = importlib.import_module("tools.metrics")
        yield module
    finally:
        with contextlib.suppress(Exception):
            import server

            server.mcp.local_provider.remove_tool(_LOCAL_FILE_TOOL)
        if original is not None:
            sys.modules["tools.metrics"] = original
        else:
            sys.modules.pop("tools.metrics", None)


def test_local_file_tool_reads_field_when_gate_enabled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With the gate on, the local-file reader returns a canonical metric from a confined file."""
    _import_metrics_tool_module()
    artifact = tmp_path / "metrics.json"
    artifact.write_bytes(json.dumps({"loss": 0.5}).encode("utf-8"))
    with _local_metrics_enabled(monkeypatch, tmp_path) as module:
        result = asyncio.run(
            module.metrics_from_local_file(path="metrics.json", field="loss", format="json")
        )
    assert "code" not in result
    assert result["metrics"] == {"loss": 0.5}
    assert result["source"] == "local_file:metrics.json"
    assert result["format"] == "json"


def test_local_file_tool_unsupported_format_envelope(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An unsupported format is rejected up front with an unsupported-format envelope."""
    _import_metrics_tool_module()
    with _local_metrics_enabled(monkeypatch, tmp_path) as module:
        result = asyncio.run(
            module.metrics_from_local_file(path="metrics.json", field="loss", format="bogus")
        )
    assert result["code"] == ErrorCode.UNSUPPORTED_FORMAT


def test_local_file_tool_invalid_aggregation_envelope(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An unknown aggregation mode is rejected before any read."""
    _import_metrics_tool_module()
    with _local_metrics_enabled(monkeypatch, tmp_path) as module:
        result = asyncio.run(
            module.metrics_from_local_file(
                path="metrics.json", field="loss", format="json", aggregation="median"
            )
        )
    assert result["code"] == ErrorCode.INVALID_AGGREGATION_MODE


def test_local_file_tool_traversal_escape_envelope(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A path escaping the configured root surfaces a confinement envelope and reads nothing."""
    _import_metrics_tool_module()
    with _local_metrics_enabled(monkeypatch, tmp_path) as module:
        result = asyncio.run(
            module.metrics_from_local_file(path="../../etc/passwd", field="loss", format="json")
        )
    assert result["code"] == ErrorCode.PATH_TRAVERSAL_ESCAPE
    assert "metrics" not in result


def test_local_file_tool_unexpected_error_maps_to_file_not_found(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A non-reader exception inside the body maps to the unreadable catch-all."""
    _import_metrics_tool_module()
    artifact = tmp_path / "metrics.json"
    artifact.write_bytes(json.dumps({"loss": 0.5}).encode("utf-8"))
    with _local_metrics_enabled(monkeypatch, tmp_path) as module:

        def _boom(*_args: object, **_kwargs: object) -> bytes:
            raise RuntimeError("read exploded")

        monkeypatch.setattr(module, "_read_local_file", _boom)
        result = asyncio.run(
            module.metrics_from_local_file(path="metrics.json", field="loss", format="json")
        )
    assert result["code"] == ErrorCode.FILE_NOT_FOUND
    assert "metrics" not in result
