"""Success-path tests for the metric-reader MCP tool wrappers.

The pure ``metric_readers`` package is exercised in isolation by its sibling
test modules; this file pins down the *tool* surface in ``gco_mcp/tools/metrics.py``
— the thin ``@mcp.tool`` wrappers a Mission session actually calls. The
success-path tests below cover every reader: each tool must return the
Canonical_Metrics_Shape — a top-level ``metrics`` object mapping the chosen key
to a single Numeric_Value, with every provenance field placed *outside*
``metrics`` — and each History_Bearing_Reader must reduce a known sequence to
the right scalar under each Aggregation_Mode (``last``, ``first``, ``min``,
``max``, ``mean``).

The readers are exercised offline. ``metrics_cloudwatch_get`` runs against a
patched ``boto3`` client (no live AWS); ``metrics_from_job_logs`` and
``metrics_from_shared_storage_file`` run against patched retrieval seams
(``cli_runner._run_cli`` / ``_read_shared_storage``) that hand back a known
artifact, so no network, AWS, or job-log access is touched.

The rest of the file builds on that success-path slice: flag-gated
registration tests, the tool-registry determinism property, the local-file
confinement integration tests, and the Observe_Phase merge-contract test.
Shared helpers are kept at module scope so those slices can reuse them.
"""

from __future__ import annotations

import asyncio
import contextlib
import importlib
import json
import os
import sys
from collections.abc import Iterator, Sequence
from datetime import datetime
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

# ``gco_mcp/run_mcp.py`` puts ``gco_mcp/`` on ``sys.path`` at runtime; mirror that here
# so the pure ``metric_readers`` package — and the ``tools.metrics`` wrappers
# that sit beside it — import the same way they do in production, matching the
# convention used by the sibling metric-reader tests.
sys.path.insert(0, str(Path(__file__).parent.parent / "gco_mcp"))

from metric_readers.shape import is_numeric_value  # noqa: E402

# Every Aggregation_Mode a History_Bearing_Reader must honour.
_MODES = ("last", "first", "min", "max", "mean")

# A known sequence whose five reductions are all distinct, so a mode that is
# silently ignored or swapped is caught: last=4, first=2, min=1, max=8,
# mean=20/5=4.0. Integers keep ``mean`` exact for a clean equality assertion.
_KNOWN_SEQUENCE = [2, 5, 1, 8, 4]


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _import_metrics_tool_module():
    """Import ``tools.metrics`` or skip when its FastMCP/server deps are absent.

    The pure ``metric_readers`` package needs nothing beyond the standard
    library, but the tool wrappers pull in ``server`` (FastMCP) and ``audit``.
    Skip rather than fail when that surface cannot be imported, so these tests
    degrade gracefully in a minimal environment — mirroring the guard used by
    ``tests/test_metric_readers_files.py``.
    """
    try:
        import tools.metrics as metrics_module
    except Exception as exc:  # noqa: BLE001 - any import-surface failure -> skip
        pytest.skip(f"tools.metrics not importable in this environment: {exc}")
    return metrics_module


def _reference_reduce(values: Sequence[float], mode: str) -> float:
    """Reduce ``values`` the obvious way for each mode.

    A deliberately straightforward re-derivation of the Aggregation_Mode
    contract, independent of the reader's own reducer, so a divergence points
    at the tool under test rather than at clever test logic. ``values`` is
    assumed already numeric (the test sequences are).
    """
    if mode == "last":
        return values[-1]
    if mode == "first":
        return values[0]
    if mode == "min":
        return min(values)
    if mode == "max":
        return max(values)
    # mode == "mean"
    return sum(values) / len(values)


def _assert_canonical_shape(
    result: dict,
    *,
    expected_key: str,
    expected_value: float,
    provenance_keys: Sequence[str],
) -> None:
    """Assert ``result`` is the Canonical_Metrics_Shape carrying one numeric.

        Pins down every clause a success result must satisfy:
        a top-level ``metrics`` object mapping exactly ``expected_key`` to a single
        Numeric_Value equal to ``expected_value``, the value passing the
        Numeric_Value guard, no error-envelope ``code`` key, and every named
        provenance field present at the top level but **never** inside ``metrics``
    .
    """
    # Not an error envelope.
    assert "code" not in result, f"expected success shape, got envelope: {result!r}"

    # Top-level metrics object mapping the key to the numeric value.
    assert "metrics" in result, f"missing top-level 'metrics': {result!r}"
    metrics = result["metrics"]
    assert isinstance(metrics, dict)
    assert metrics == {expected_key: expected_value}, (
        f"expected metrics {{{expected_key!r}: {expected_value!r}}}, got {metrics!r}"
    )

    # The emitted value is a real, finite number.
    assert is_numeric_value(metrics[expected_key])

    # Provenance lives outside ``metrics``: present at the top level and
    # absent from the metrics object, which carries only the numeric entry.
    for prov_key in provenance_keys:
        assert prov_key in result, f"missing provenance field {prov_key!r}: {result!r}"
        assert prov_key not in metrics, f"provenance {prov_key!r} leaked into metrics"


# ===========================================================================
# CloudWatch reader — success path (single deterministic datapoint)
# ===========================================================================


def _cloudwatch_client_returning(datapoints: list[dict]) -> MagicMock:
    """Build a mock boto3 CloudWatch client returning ``datapoints``.

    The mock stands in for ``boto3.client("cloudwatch", ...)``; its
    ``get_metric_statistics`` returns the supplied datapoints so no live AWS
    call is made.
    """
    client = MagicMock()
    client.get_metric_statistics.return_value = {"Datapoints": datapoints}
    return client


def test_cloudwatch_get_success_returns_canonical_shape() -> None:
    """The CloudWatch reader emits the most-recent datapoint as a canonical metric.

    With ``boto3`` patched to hand back two datapoints carrying distinct
    timestamps, the tool must select the most recent one deterministically and
    return its statistic value in the Canonical_Metrics_Shape under the metric
    key (defaulting to the CloudWatch metric name), with the source / region /
    statistic / timestamp provenance outside ``metrics``.
    """
    metrics_module = _import_metrics_tool_module()

    older = {"Timestamp": datetime(2024, 1, 1, 0, 0, 0), "Average": 0.91}
    newer = {"Timestamp": datetime(2024, 1, 1, 1, 0, 0), "Average": 0.42}
    client = _cloudwatch_client_returning([older, newer])

    with patch("boto3.client", return_value=client) as mock_client:
        result = asyncio.run(
            metrics_module.metrics_cloudwatch_get(
                metric_name="GpuUtilization",
                namespace="GCO/Training",
                region="us-east-1",
                dimensions={"JobName": "sft-run"},
                statistic="Average",
            )
        )

    # No live AWS: the region-scoped client was constructed and a read-only
    # GetMetricStatistics was issued against the mock.
    mock_client.assert_called_once()
    assert mock_client.call_args.kwargs.get("region_name") == "us-east-1"
    client.get_metric_statistics.assert_called_once()

    # Most-recent datapoint (0.42 at 01:00) wins over the older (0.91 at 00:00).
    _assert_canonical_shape(
        result,
        expected_key="GpuUtilization",
        expected_value=0.42,
        provenance_keys=("source", "region", "statistic", "datapoint_timestamp"),
    )
    assert result["region"] == "us-east-1"
    assert result["statistic"] == "Average"


def test_cloudwatch_get_success_honours_output_name() -> None:
    """An explicit ``output_name`` becomes the metric key on the success path."""
    metrics_module = _import_metrics_tool_module()

    client = _cloudwatch_client_returning(
        [{"Timestamp": datetime(2024, 6, 1, 12, 0, 0), "Sum": 1234}]
    )

    with patch("boto3.client", return_value=client):
        result = asyncio.run(
            metrics_module.metrics_cloudwatch_get(
                metric_name="RequestCount",
                namespace="GCO/Serving",
                region="us-west-2",
                statistic="Sum",
                output_name="throughput",
            )
        )

    _assert_canonical_shape(
        result,
        expected_key="throughput",
        expected_value=1234,
        provenance_keys=("source", "region", "statistic", "datapoint_timestamp"),
    )


# ===========================================================================
# Job-log reader — success path + every Aggregation_Mode
# ===========================================================================


def _patch_job_logs(metrics_module, monkeypatch: pytest.MonkeyPatch, lines: list[str]) -> None:
    """Patch the job-log retrieval seam to return ``lines`` as log text.

    The tool fetches logs through ``cli_runner._run_cli("jobs", "logs", ...)``;
    replacing that call with one that returns the joined lines exercises the
    reader end-to-end without any job-log access.
    """

    def _fake_run_cli(*_args: object, **_kwargs: object) -> str:
        return "\n".join(lines)

    monkeypatch.setattr(metrics_module.cli_runner, "_run_cli", _fake_run_cli)


@pytest.mark.parametrize("mode", _MODES)
def test_job_logs_json_key_reduces_known_sequence(
    mode: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The job-log reader reduces a known per-line sequence under each mode.

    Each log line is a JSON object carrying one ``loss`` value from the known
    sequence; the reader extracts the dot-path key, reduces under ``mode``, and
    returns the reference reduction in the Canonical_Metrics_Shape (key defaults
    to the JSON key's last segment), with aggregation/source provenance outside
    ``metrics``.
    """
    metrics_module = _import_metrics_tool_module()
    lines = [json.dumps({"loss": v}) for v in _KNOWN_SEQUENCE]
    _patch_job_logs(metrics_module, monkeypatch, lines)

    result = asyncio.run(
        metrics_module.metrics_from_job_logs(
            job_name="sft-run",
            region="us-east-1",
            json_key="loss",
            aggregation=mode,
        )
    )

    expected = _reference_reduce(_KNOWN_SEQUENCE, mode)
    _assert_canonical_shape(
        result,
        expected_key="loss",
        expected_value=expected,
        provenance_keys=("source", "region", "aggregation", "match_count"),
    )
    assert result["aggregation"] == mode
    assert result["match_count"] == len(_KNOWN_SEQUENCE)


def test_job_logs_default_aggregation_is_last(monkeypatch: pytest.MonkeyPatch) -> None:
    """With no aggregation supplied the job-log reader returns the most-recent match."""
    metrics_module = _import_metrics_tool_module()
    lines = [json.dumps({"loss": v}) for v in _KNOWN_SEQUENCE]
    _patch_job_logs(metrics_module, monkeypatch, lines)

    result = asyncio.run(
        metrics_module.metrics_from_job_logs(
            job_name="sft-run",
            region="us-east-1",
            json_key="loss",
        )
    )

    _assert_canonical_shape(
        result,
        expected_key="loss",
        expected_value=_KNOWN_SEQUENCE[-1],
        provenance_keys=("source", "region", "aggregation", "match_count"),
    )
    assert result["aggregation"] == "last"


@pytest.mark.parametrize("mode", _MODES)
def test_job_logs_regex_reduces_known_sequence(mode: str, monkeypatch: pytest.MonkeyPatch) -> None:
    """Regex extraction's first capture group reduces under each mode.

    The free-text lines print ``loss=<n>``; the reader captures the first group
    per line, coerces to a number, and reduces under ``mode`` to the reference
    scalar in the canonical shape (key defaults to ``value`` in regex mode).
    """
    metrics_module = _import_metrics_tool_module()
    lines = [f"step done loss={v}" for v in _KNOWN_SEQUENCE]
    _patch_job_logs(metrics_module, monkeypatch, lines)

    result = asyncio.run(
        metrics_module.metrics_from_job_logs(
            job_name="sft-run",
            region="us-east-1",
            regex=r"loss=([0-9.]+)",
            aggregation=mode,
        )
    )

    expected = _reference_reduce(_KNOWN_SEQUENCE, mode)
    _assert_canonical_shape(
        result,
        expected_key="value",
        expected_value=expected,
        provenance_keys=("source", "region", "aggregation", "match_count"),
    )


# ===========================================================================
# Shared-storage file reader — success path + every Aggregation_Mode
# ===========================================================================


def _patch_shared_storage(metrics_module, monkeypatch: pytest.MonkeyPatch, content: bytes) -> None:
    """Patch the shared-storage read seam to return ``content``.

    The tool fetches the artifact via ``_read_shared_storage(path, region,
    max_bytes)``; replacing it with one that returns known bytes exercises the
    reader (format dispatch + reduction + shape building) without any storage
    or network access.
    """

    def _fake_read(_path: str, _region: str, _max_bytes: int) -> bytes:
        return content

    monkeypatch.setattr(metrics_module, "_read_shared_storage", _fake_read)


def test_shared_storage_single_value_returns_canonical_shape(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A single JSON scalar reads back as a canonical metric.

    The non-history success path: a plain ``json`` document whose field is one
    number returns that number in the Canonical_Metrics_Shape with the
    source / region / format / aggregation provenance outside ``metrics``.
    """
    metrics_module = _import_metrics_tool_module()
    content = json.dumps({"loss": 0.125}).encode("utf-8")
    _patch_shared_storage(metrics_module, monkeypatch, content)

    result = asyncio.run(
        metrics_module.metrics_from_shared_storage_file(
            path="runs/metrics.json",
            region="us-east-1",
            field="loss",
            format="json",
        )
    )

    _assert_canonical_shape(
        result,
        expected_key="loss",
        expected_value=0.125,
        provenance_keys=("source", "region", "format", "aggregation"),
    )
    assert result["format"] == "json"


@pytest.mark.parametrize("mode", _MODES)
def test_shared_storage_hf_log_history_reduces_known_sequence(
    mode: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A Hugging Face ``log_history`` reduces under each mode.

    The ``hf_trainer_state`` format gathers one ``loss`` per ``log_history``
    entry from the known sequence and reduces under ``mode`` to the reference
    scalar in the canonical shape — the sequence-bearing success path for the
    file reader.
    """
    metrics_module = _import_metrics_tool_module()
    content = json.dumps({"log_history": [{"loss": v} for v in _KNOWN_SEQUENCE]}).encode("utf-8")
    _patch_shared_storage(metrics_module, monkeypatch, content)

    result = asyncio.run(
        metrics_module.metrics_from_shared_storage_file(
            path="runs/trainer_state.json",
            region="us-east-1",
            field="loss",
            format="hf_trainer_state",
            aggregation=mode,
        )
    )

    expected = _reference_reduce(_KNOWN_SEQUENCE, mode)
    _assert_canonical_shape(
        result,
        expected_key="loss",
        expected_value=expected,
        provenance_keys=("source", "region", "format", "aggregation"),
    )
    assert result["aggregation"] == mode


@pytest.mark.parametrize("mode", _MODES)
def test_shared_storage_jsonl_reduces_known_sequence(
    mode: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A JSONL step-log stream reduces under each mode.

    The ``jsonl`` format gathers the named field from each record of the known
    sequence and reduces under ``mode`` to the reference scalar in the
    canonical shape, covering a second sequence-bearing format.
    """
    metrics_module = _import_metrics_tool_module()
    content = "\n".join(json.dumps({"loss": v}) for v in _KNOWN_SEQUENCE).encode("utf-8")
    _patch_shared_storage(metrics_module, monkeypatch, content)

    result = asyncio.run(
        metrics_module.metrics_from_shared_storage_file(
            path="runs/steps.jsonl",
            region="us-east-1",
            field="loss",
            format="jsonl",
            aggregation=mode,
        )
    )

    expected = _reference_reduce(_KNOWN_SEQUENCE, mode)
    _assert_canonical_shape(
        result,
        expected_key="loss",
        expected_value=expected,
        provenance_keys=("source", "region", "format", "aggregation"),
    )


# ===========================================================================
# Flag-gated registration
# ===========================================================================
#
# The success-path slice above pins down what each reader *returns*. This slice
# pins down which readers are *registered* on the shared FastMCP server as a
# function of the gating flags. The contract:
#
#   * the three default-on readers (``metrics_cloudwatch_get``,
#     ``metrics_from_job_logs``, ``metrics_from_shared_storage_file``) are
#     always present, independent of any flag;
#   * ``metrics_from_local_file`` is default-off — absent when neither
#     ``GCO_ENABLE_LOCAL_METRICS`` nor the umbrella ``GCO_ENABLE_ALL_TOOLS`` is
#     set, present when ``GCO_ENABLE_LOCAL_METRICS=true`` or the
#     umbrella ``GCO_ENABLE_ALL_TOOLS=true`` is set with the local flag unset;
#   * when present, its description begins with the literal prefix
#     ``"[gated by GCO_ENABLE_LOCAL_METRICS]"`` and it carries the
#     ``safe`` Tool_Tag.
#
# Registry introspection note: the server (``gco_mcp/server.py``) wires the
# BM25/Code-Mode catalog-replacement transforms, so the *public*
# ``mcp.list_tools()`` only exposes ~5 synthetic tools. To assert *real*
# registration we go through the underlying registry via the private
# ``mcp._list_tools()`` — exactly as ``tests/test_mcp_server.py::
# TestToolRegistration`` does.

# The three readers that must always be registered regardless of any flag.
_DEFAULT_ON_READERS = (
    "metrics_cloudwatch_get",
    "metrics_from_job_logs",
    "metrics_from_shared_storage_file",
)

_LOCAL_FILE_TOOL = "metrics_from_local_file"
_GATED_DOCSTRING_PREFIX = "[gated by GCO_ENABLE_LOCAL_METRICS]"


def _registered_tools() -> dict:
    """Return the real registry as a ``{name: Tool}`` map.

    Uses the private ``mcp._list_tools()`` to bypass the BM25/Code-Mode
    catalog-replacement transforms — the public ``list_tools()`` would only
    ever show the handful of synthetic entry-point tools regardless of what is
    registered. ``run_mcp`` imports the shared FastMCP singleton and fires
    ``register_all_tools()`` (which imports ``tools.metrics``) at import time,
    so the default-on readers are registered as a side effect of importing it.
    """
    import run_mcp

    tools = asyncio.run(run_mcp.mcp._list_tools())
    return {t.name: t for t in tools}


@contextlib.contextmanager
def _local_metrics_flag(env: dict[str, str | None]) -> Iterator[dict]:
    """Re-import ``tools.metrics`` under ``env`` and yield the live registry.

    ``metrics_from_local_file`` is registered by a module-level
    ``if is_enabled("GCO_ENABLE_LOCAL_METRICS"):`` block, so toggling its
    registration requires setting the relevant env vars and **re-importing**
    ``tools.metrics`` so the gate is re-evaluated. ``env`` maps env-var names to
    values (or ``None`` to unset). After the body runs, the gated tool is
    force-unregistered from the shared FastMCP singleton and the environment is
    restored, so a registration here never leaks into a sibling test's
    tool-count / tool-name snapshot (the same teardown precedent used by the
    destructive-gating and local-fs confinement tests).

    Yields the ``{name: Tool}`` registry map captured *after* the re-import.
    """
    previous: dict[str, str | None] = {key: os.environ.get(key) for key in env}
    for key, value in env.items():
        if value is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = value

    # Re-import under the env so the gated decorator re-evaluates its flag.
    if "tools.metrics" in sys.modules:
        del sys.modules["tools.metrics"]
    importlib.import_module("tools.metrics")

    try:
        yield _registered_tools()
    finally:
        # Drop the gated registration off the shared singleton so the canonical
        # tool-name / tool-count snapshots in sibling tests stay clean.
        with contextlib.suppress(Exception):
            import server

            server.mcp.local_provider.remove_tool(_LOCAL_FILE_TOOL)
        # Restore the environment we mutated.
        for key, value in previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def test_local_file_reader_absent_when_flag_unset() -> None:
    """With the gate unset, ``metrics_from_local_file`` is not registered.

    With neither ``GCO_ENABLE_LOCAL_METRICS`` nor the umbrella
    ``GCO_ENABLE_ALL_TOOLS`` set, the gated decorator never fires and the tool
    is absent from the registry; the three default-on readers remain present.
    """
    _import_metrics_tool_module()
    env = {"GCO_ENABLE_LOCAL_METRICS": None, "GCO_ENABLE_ALL_TOOLS": None}
    with _local_metrics_flag(env) as registry:
        assert _LOCAL_FILE_TOOL not in registry, (
            "metrics_from_local_file must be absent when the gate is unset"
        )
        for reader in _DEFAULT_ON_READERS:
            assert reader in registry, f"default-on reader {reader!r} must always register"


def test_local_file_reader_present_when_local_flag_set() -> None:
    """``GCO_ENABLE_LOCAL_METRICS=true`` registers ``metrics_from_local_file``.

    Setting the per-tool flag fires the gated decorator so the tool appears in
    the registry; the default-on readers stay present alongside it.
    """
    _import_metrics_tool_module()
    env = {"GCO_ENABLE_LOCAL_METRICS": "true", "GCO_ENABLE_ALL_TOOLS": None}
    with _local_metrics_flag(env) as registry:
        assert _LOCAL_FILE_TOOL in registry, (
            "metrics_from_local_file must register when GCO_ENABLE_LOCAL_METRICS=true"
        )
        for reader in _DEFAULT_ON_READERS:
            assert reader in registry, f"default-on reader {reader!r} must always register"


def test_local_file_reader_present_under_umbrella_flag() -> None:
    """The umbrella ``GCO_ENABLE_ALL_TOOLS=true`` also registers the gated reader.

    With the per-tool ``GCO_ENABLE_LOCAL_METRICS`` left unset, setting only the
    umbrella ``GCO_ENABLE_ALL_TOOLS=true`` must still enable the tool, mirroring
    the umbrella-override semantics every other per-tool flag inherits from
    ``feature_flags.is_enabled``.
    """
    _import_metrics_tool_module()
    env = {"GCO_ENABLE_LOCAL_METRICS": None, "GCO_ENABLE_ALL_TOOLS": "true"}
    with _local_metrics_flag(env) as registry:
        assert _LOCAL_FILE_TOOL in registry, (
            "metrics_from_local_file must register under the umbrella "
            "GCO_ENABLE_ALL_TOOLS=true with the per-tool flag unset"
        )
        for reader in _DEFAULT_ON_READERS:
            assert reader in registry, f"default-on reader {reader!r} must always register"


def test_local_file_reader_docstring_prefix_and_safe_tag() -> None:
    """When registered, the gated reader is prefixed and carries the ``safe`` tag.

    With the gate on, the tool's description must begin with the literal prefix
    ``"[gated by GCO_ENABLE_LOCAL_METRICS]"`` and its Tool_Tag set must
    include ``safe``, marking it read-only like its siblings.
    """
    _import_metrics_tool_module()
    env = {"GCO_ENABLE_LOCAL_METRICS": "true", "GCO_ENABLE_ALL_TOOLS": None}
    with _local_metrics_flag(env) as registry:
        tool = registry.get(_LOCAL_FILE_TOOL)
        assert tool is not None, "metrics_from_local_file must register under the flag"

        # The description begins with the literal gating prefix.
        assert tool.description is not None
        assert tool.description.startswith(_GATED_DOCSTRING_PREFIX), (
            f"description must begin with {_GATED_DOCSTRING_PREFIX!r}, "
            f"got {tool.description[: len(_GATED_DOCSTRING_PREFIX)]!r}"
        )

        # It carries the read-only ``safe`` Tool_Tag.
        assert "safe" in tool.tags, f"expected 'safe' tag, got {tool.tags!r}"


def test_default_on_readers_present_regardless_of_flag() -> None:
    """The three default-on readers register whether the gate is on or off.

    A default-registered Metric_Reader_Tool must appear in the registry
    independent of any flag. Toggling ``GCO_ENABLE_LOCAL_METRICS`` only adds or
    removes the gated local-file reader; it must never change the presence of
    the three default-on readers.
    """
    _import_metrics_tool_module()

    # Flag off: default-on readers present, gated reader absent.
    with _local_metrics_flag(
        {"GCO_ENABLE_LOCAL_METRICS": None, "GCO_ENABLE_ALL_TOOLS": None}
    ) as registry_off:
        for reader in _DEFAULT_ON_READERS:
            assert reader in registry_off, (
                f"default-on reader {reader!r} must be present with the gate off"
            )
        assert _LOCAL_FILE_TOOL not in registry_off

    # Flag on: default-on readers still present, gated reader now present too.
    with _local_metrics_flag(
        {"GCO_ENABLE_LOCAL_METRICS": "true", "GCO_ENABLE_ALL_TOOLS": None}
    ) as registry_on:
        for reader in _DEFAULT_ON_READERS:
            assert reader in registry_on, (
                f"default-on reader {reader!r} must be present with the gate on"
            )
        assert _LOCAL_FILE_TOOL in registry_on


# ===========================================================================
# Tool-registry determinism
# ===========================================================================
#
# The flag-gated slice above pins down individual flag combinations with hand
# written examples. This slice generalises that contract across the whole
# Feature_Flag space with a property test: for *any* combination of the two
# flags that govern the local-file reader, the registry must be a deterministic
# function of those flags. Concretely, for each generated combination:
#
#   1. Determinism — re-running ``_list_tools()`` under the SAME flag
#      values yields the IDENTICAL set of tool names. The Tool_Registry must
#      not depend on call order, time, or any hidden state.
#   2. Default-on presence — the three default-on readers are ALWAYS in
#      the registry, independent of the flags.
#   3. Gated presence — ``metrics_from_local_file`` is present
#      IFF the gate is enabled, i.e. ``GCO_ENABLE_LOCAL_METRICS=true`` OR the
#      umbrella ``GCO_ENABLE_ALL_TOOLS=true`` — exactly the
#      ``feature_flags.is_enabled`` truth rule.
#
# Each example flips env vars and re-imports ``tools.metrics`` (slow), so the
# settings mirror the sibling metric-reader property tests: ``deadline=None``
# and the ``too_slow``/``data_too_large`` health checks suppressed. The
# ``_local_metrics_flag`` context manager (defined above for the flag-gated
# registration tests) is used
# as a context manager *inside* the test body so its force-unregister +
# environ-restore teardown runs for EVERY example — no gated registration ever
# leaks into a sibling test's tool-count / tool-name snapshot.

# The flag values an example can draw for each gate. ``None`` means "unset";
# the non-"true" string exercises the "set but not enabled" branch of the
# ``is_enabled`` truth rule (only the literal "true", stripped/lowered, enables).
_FLAG_VALUES = st.sampled_from([None, "true", "false"])


def _gate_expected_present(env: dict[str, str | None]) -> bool:
    """Re-derive ``is_enabled('GCO_ENABLE_LOCAL_METRICS')`` from ``env``.

    An independent restatement of the ``feature_flags`` truth rule: the gate is
    enabled iff the umbrella ``GCO_ENABLE_ALL_TOOLS`` OR the per-tool
    ``GCO_ENABLE_LOCAL_METRICS`` is the literal ``"true"`` (case-insensitive,
    stripped). Kept deliberately separate from the production helper so a
    divergence points at the tool gating rather than at shared logic.
    """

    def _is_true(value: str | None) -> bool:
        return value is not None and value.strip().lower() == "true"

    return _is_true(env.get("GCO_ENABLE_ALL_TOOLS")) or _is_true(
        env.get("GCO_ENABLE_LOCAL_METRICS")
    )


@settings(
    max_examples=100,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow, HealthCheck.data_too_large],
)
@given(local_flag=_FLAG_VALUES, umbrella_flag=_FLAG_VALUES)
def test_tool_registry_determinism(local_flag: str | None, umbrella_flag: str | None) -> None:
    """The Tool_Registry is a deterministic function of the gating flags.

    For any combination of ``GCO_ENABLE_LOCAL_METRICS`` and the umbrella
    ``GCO_ENABLE_ALL_TOOLS``, re-importing ``tools.metrics`` under those values
    yields a registry whose tool-name set is stable across repeated
    ``_list_tools()`` calls, always contains the three default-on
    readers, and contains ``metrics_from_local_file`` iff the gate is
    enabled.
    """
    _import_metrics_tool_module()

    env = {
        "GCO_ENABLE_LOCAL_METRICS": local_flag,
        "GCO_ENABLE_ALL_TOOLS": umbrella_flag,
    }

    # The context manager re-imports under ``env`` and, on exit, force
    # unregisters the gated tool and restores the environment — so this example
    # cannot leak gated registration into the next example or a sibling test.
    with _local_metrics_flag(env) as registry:
        names_first = set(registry)

        # (1) Determinism: a second introspection under the unchanged
        # flag values returns the identical set of tool names.
        names_second = set(_registered_tools())
        assert names_first == names_second, (
            "repeated _list_tools() under fixed flags must return the identical "
            f"tool-name set; first={names_first ^ names_second} differed"
        )

        # (2) Default-on presence: the three readers are always present,
        # independent of the flags.
        for reader in _DEFAULT_ON_READERS:
            assert reader in names_first, (
                f"default-on reader {reader!r} must always be registered, flags={env!r}"
            )

        # (3) Gated presence: the local-file reader is present
        # iff the gate is enabled per the is_enabled truth rule.
        expected_present = _gate_expected_present(env)
        assert (_LOCAL_FILE_TOOL in names_first) == expected_present, (
            f"metrics_from_local_file presence ({_LOCAL_FILE_TOOL in names_first}) "
            f"must equal gate-enabled ({expected_present}) for flags={env!r}"
        )
