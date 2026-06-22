"""Tests for the CloudWatch datapoint reader.

A CloudWatch ``GetMetricStatistics`` response can carry several datapoints —
one per aggregation period inside the requested window. The reader has to pick
exactly one, deterministically, and it picks the freshest: the datapoint whose
``Timestamp`` is the latest. ``select_most_recent`` is the pure helper that
does this, returning that datapoint's statistic reading (as a float) together
with its timestamp rendered in ISO-8601 form.

The property test below pins down that "latest wins" contract across a wide
range of datapoint lists with distinct timestamps and mixed int/float
readings: whatever the input order, the value and timestamp handed back must
belong to the datapoint with the maximum ``Timestamp``.
"""

from __future__ import annotations

import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from botocore.exceptions import BotoCoreError, ClientError, EndpointConnectionError
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

# ``gco_mcp/run_mcp.py`` puts ``gco_mcp/`` on ``sys.path`` at runtime; mirror that
# here so the pure ``metric_readers`` package imports the same way it does
# in production, matching the convention used by the sibling Mission tests.
sys.path.insert(0, str(Path(__file__).parent.parent / "gco_mcp"))

from metric_readers.cloudwatch import get_datapoint, select_most_recent  # noqa: E402
from metric_readers.shape import ErrorCode, MetricReaderError  # noqa: E402

# ---------------------------------------------------------------------------
# Strategies
# ---------------------------------------------------------------------------

# The statistic key carried by each datapoint. These are the read-only
# statistics CloudWatch can return; the reader reads whichever one was asked
# for, so every datapoint in a single example carries the same key.
_statistics = st.sampled_from(["Average", "Sum", "Maximum", "Minimum", "SampleCount"])

# A real, finite statistic reading: an int (never a bool) or a finite float.
_readings = st.one_of(
    st.integers(),
    st.floats(allow_nan=False, allow_infinity=False),
)


@st.composite
def _datapoints_with_distinct_timestamps(draw: st.DrawFn) -> tuple[str, list[dict[str, Any]]]:
    """Draw a non-empty datapoint list with strictly distinct timestamps.

    Each datapoint is a dict shaped like a CloudWatch datapoint: a
    timezone-aware UTC ``Timestamp`` plus one statistic key mapping to a
    finite number. Timestamps are unique within the list so the latest
    datapoint is unambiguous, mirroring real ``GetMetricStatistics`` output
    where each period yields at most one datapoint.
    """
    statistic = draw(_statistics)
    timestamps = draw(
        st.lists(
            st.datetimes(timezones=st.just(UTC)),
            min_size=1,
            max_size=20,
            unique=True,
        )
    )
    datapoints = [{"Timestamp": timestamp, statistic: draw(_readings)} for timestamp in timestamps]
    # Shuffle so the helper cannot accidentally rely on input ordering.
    draw(st.randoms(use_true_random=False)).shuffle(datapoints)
    return statistic, datapoints


# ---------------------------------------------------------------------------
# Property: the latest datapoint wins
# ---------------------------------------------------------------------------


@settings(
    max_examples=200,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow, HealthCheck.data_too_large],
)
@given(_datapoints_with_distinct_timestamps())
def test_select_most_recent_returns_latest_datapoint(
    case: tuple[str, list[dict[str, Any]]],
) -> None:
    """``select_most_recent`` returns the value and ISO timestamp of the latest datapoint.

    For any non-empty datapoint list with distinct timestamps, the helper must
    return the statistic reading (coerced to float) and the ISO-8601 timestamp
    of the datapoint whose ``Timestamp`` is the maximum — regardless of the
    order the datapoints arrive in.
    """
    statistic, datapoints = case

    # Independent reference: order by timestamp and take the last one. With
    # distinct timestamps this is exactly the unique maximum.
    latest = sorted(datapoints, key=lambda dp: dp["Timestamp"])[-1]
    expected_value = float(latest[statistic])
    expected_iso = latest["Timestamp"].isoformat()

    value, iso_timestamp = select_most_recent(datapoints, statistic)

    assert value == expected_value
    assert isinstance(value, float)
    assert iso_timestamp == expected_iso


# ---------------------------------------------------------------------------
# Mocked-CloudWatch unit tests for ``get_datapoint`` (the boto3 wrapper)
# ---------------------------------------------------------------------------
#
# ``get_datapoint`` does a lazy ``import boto3`` inside the function and then
# calls ``boto3.client("cloudwatch", region_name=region)``. Patching
# ``boto3.client`` at the boto3 module level therefore intercepts the client
# construction without any live AWS call being made. Each test below builds a
# ``MagicMock`` client, wires its ``get_metric_statistics`` to either return a
# canned response or raise a botocore exception, and asserts the read-only call
# shape and the structured-error translation.

# A fixed, timezone-aware lookback window reused across the call-shape tests so
# the asserted ``StartTime``/``EndTime`` are unambiguous.
_START = datetime(2024, 1, 1, 12, 0, 0, tzinfo=UTC)
_END = datetime(2024, 1, 1, 13, 0, 0, tzinfo=UTC)


def _patched_client(mock_client: MagicMock) -> Any:
    """Patch ``boto3.client`` so it returns ``mock_client``.

    Returns the patcher object so the caller can use it as a context manager
    and inspect the ``boto3.client`` mock (e.g. to assert the region scoping).
    """
    return patch("boto3.client", return_value=mock_client)


def test_get_datapoint_issues_readonly_call_with_exact_shape() -> None:
    """The wrapper scopes the client to the region and passes the exact read-only args.

    On the success path ``get_datapoint`` must construct a region-scoped
    CloudWatch client and issue a single ``get_metric_statistics`` call whose
    arguments mirror the inputs unchanged: namespace, metric name, dimensions
    (as ``{"Name","Value"}`` pairs), the lookback window, period, and the
    single requested statistic. No mutating CloudWatch API may be touched.
    """
    mock_client = MagicMock()
    mock_client.get_metric_statistics.return_value = {
        "Datapoints": [{"Timestamp": _END, "Average": 42.5}]
    }

    with _patched_client(mock_client) as mock_boto_client:
        value, iso_timestamp = get_datapoint(
            metric_name="GPUUtilization",
            namespace="GCO/Training",
            dimensions={"JobName": "megatrain", "Region": "us-east-1"},
            region="us-west-2",
            period=300,
            statistic="Average",
            start_time=_START,
            end_time=_END,
        )

    # The client is scoped to the requested region (and only the cloudwatch service).
    mock_boto_client.assert_called_once_with("cloudwatch", region_name="us-west-2")

    # Exactly one CloudWatch call, and it is the read-only GetMetricStatistics.
    assert [c[0] for c in mock_client.method_calls] == ["get_metric_statistics"]
    mock_client.get_metric_statistics.assert_called_once_with(
        Namespace="GCO/Training",
        MetricName="GPUUtilization",
        Dimensions=[
            {"Name": "JobName", "Value": "megatrain"},
            {"Name": "Region", "Value": "us-east-1"},
        ],
        StartTime=_START,
        EndTime=_END,
        Period=300,
        Statistics=["Average"],
    )

    # And the selected datapoint flows back out unchanged.
    assert value == 42.5
    assert iso_timestamp == _END.isoformat()


def test_get_datapoint_passes_none_dimensions_as_empty_list() -> None:
    """``dimensions=None`` is sent to CloudWatch as an empty dimension list."""
    mock_client = MagicMock()
    mock_client.get_metric_statistics.return_value = {"Datapoints": [{"Timestamp": _END, "Sum": 7}]}

    with _patched_client(mock_client):
        get_datapoint(
            metric_name="Throughput",
            namespace="GCO/Training",
            dimensions=None,
            region="eu-central-1",
            period=60,
            statistic="Sum",
            start_time=_START,
            end_time=_END,
        )

    _, kwargs = mock_client.get_metric_statistics.call_args
    assert kwargs["Dimensions"] == []
    assert kwargs["Statistics"] == ["Sum"]


def test_get_datapoint_zero_datapoints_raises_no_datapoints() -> None:
    """An empty ``Datapoints`` list maps to ``NO_DATAPOINTS`` (no metrics shape)."""
    mock_client = MagicMock()
    mock_client.get_metric_statistics.return_value = {"Datapoints": []}

    with _patched_client(mock_client), pytest.raises(MetricReaderError) as exc_info:
        get_datapoint(
            metric_name="Loss",
            namespace="GCO/Training",
            dimensions={"JobName": "megatrain"},
            region="us-east-1",
            period=300,
            statistic="Average",
            start_time=_START,
            end_time=_END,
        )

    error = exc_info.value
    assert error.code == ErrorCode.NO_DATAPOINTS
    assert error.code == "no_datapoints"
    assert error.details is not None
    assert error.details["metric_name"] == "Loss"
    assert error.details["namespace"] == "GCO/Training"
    assert error.details["region"] == "us-east-1"
    assert error.details["statistic"] == "Average"


def test_get_datapoint_client_error_maps_to_aws_unreachable_client_error_kind() -> None:
    """A generic ``ClientError`` maps to ``AWS_UNREACHABLE`` with ``kind='client_error'``."""
    mock_client = MagicMock()
    mock_client.get_metric_statistics.side_effect = ClientError(
        {"Error": {"Code": "Throttling", "Message": "Rate exceeded"}},
        "GetMetricStatistics",
    )

    with _patched_client(mock_client), pytest.raises(MetricReaderError) as exc_info:
        get_datapoint(
            metric_name="Loss",
            namespace="GCO/Training",
            dimensions=None,
            region="us-east-1",
            period=300,
            statistic="Average",
            start_time=_START,
            end_time=_END,
        )

    error = exc_info.value
    assert error.code == ErrorCode.AWS_UNREACHABLE
    assert error.code == "aws_unreachable"
    assert error.details is not None
    assert error.details["kind"] == "client_error"
    assert error.details["region"] == "us-east-1"
    assert error.details["aws_error_code"] == "Throttling"


def test_get_datapoint_access_denied_maps_to_unauthorized_kind() -> None:
    """A credentials/permissions ``ClientError`` maps to ``kind='unauthorized'``."""
    mock_client = MagicMock()
    mock_client.get_metric_statistics.side_effect = ClientError(
        {"Error": {"Code": "AccessDenied", "Message": "not authorized"}},
        "GetMetricStatistics",
    )

    with _patched_client(mock_client), pytest.raises(MetricReaderError) as exc_info:
        get_datapoint(
            metric_name="Loss",
            namespace="GCO/Training",
            dimensions=None,
            region="us-east-1",
            period=300,
            statistic="Average",
            start_time=_START,
            end_time=_END,
        )

    error = exc_info.value
    assert error.code == ErrorCode.AWS_UNREACHABLE
    assert error.details is not None
    assert error.details["kind"] == "unauthorized"
    assert error.details["aws_error_code"] == "AccessDenied"


def test_get_datapoint_endpoint_connection_error_maps_to_unreachable_kind() -> None:
    """An ``EndpointConnectionError`` maps to ``AWS_UNREACHABLE`` with ``kind='unreachable'``."""
    mock_client = MagicMock()
    mock_client.get_metric_statistics.side_effect = EndpointConnectionError(
        endpoint_url="https://monitoring.us-east-1.amazonaws.com"
    )

    with _patched_client(mock_client), pytest.raises(MetricReaderError) as exc_info:
        get_datapoint(
            metric_name="Loss",
            namespace="GCO/Training",
            dimensions=None,
            region="us-east-1",
            period=300,
            statistic="Average",
            start_time=_START,
            end_time=_END,
        )

    error = exc_info.value
    assert error.code == ErrorCode.AWS_UNREACHABLE
    assert error.details is not None
    assert error.details["kind"] == "unreachable"
    assert error.details["region"] == "us-east-1"


def test_get_datapoint_botocore_error_maps_to_unreachable_kind() -> None:
    """A generic ``BotoCoreError`` maps to ``AWS_UNREACHABLE`` with ``kind='unreachable'``."""
    mock_client = MagicMock()
    mock_client.get_metric_statistics.side_effect = BotoCoreError()

    with _patched_client(mock_client), pytest.raises(MetricReaderError) as exc_info:
        get_datapoint(
            metric_name="Loss",
            namespace="GCO/Training",
            dimensions=None,
            region="ap-southeast-2",
            period=300,
            statistic="Average",
            start_time=_START,
            end_time=_END,
        )

    error = exc_info.value
    assert error.code == ErrorCode.AWS_UNREACHABLE
    assert error.details is not None
    assert error.details["kind"] == "unreachable"
    assert error.details["region"] == "ap-southeast-2"
