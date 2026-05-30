"""Read-only CloudWatch datapoint reader.

This module turns a single CloudWatch ``GetMetricStatistics`` request into one
numeric scalar a metric-threshold check can read. It has two pieces:

* :func:`select_most_recent` — a pure helper that, given a non-empty list of
  CloudWatch datapoints, picks the one with the latest timestamp and returns
  its statistic value alongside that timestamp in ISO-8601 form. No I/O, so it
  is trivial to test in isolation.
* :func:`get_datapoint` — the thin boto3 wrapper. It constructs a
  region-scoped CloudWatch client, issues one read-only
  ``GetMetricStatistics`` call bounded to the requested window, and hands the
  returned datapoints to :func:`select_most_recent`. Every failure mode —
  an empty result, an unreachable endpoint, a credentials problem, or any
  other client error — is translated into a :class:`MetricReaderError` with a
  stable code so the calling tool can render a structured error envelope
  instead of crashing.

``boto3`` is imported lazily inside :func:`get_datapoint` (mirroring the
lazy-import convention used by the SSM helpers and the stacks CLI) so this
module's import surface stays free of the SDK and tests can monkeypatch
``boto3.client`` without dragging the SDK into unrelated test runs.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from .shape import ErrorCode, MetricReaderError

# CloudWatch ``Error.Code`` values that mean "the request was rejected because
# of credentials or permissions" rather than a transient transport failure.
# These map to the ``unauthorized`` discriminator so an operator can tell an
# access problem apart from an unreachable endpoint without reading the message.
_UNAUTHORIZED_ERROR_CODES = frozenset(
    {
        "AccessDenied",
        "AccessDeniedException",
        "AuthFailure",
        "ExpiredToken",
        "ExpiredTokenException",
        "InvalidAccessKeyId",
        "InvalidClientTokenId",
        "SignatureDoesNotMatch",
        "UnauthorizedOperation",
        "UnrecognizedClientException",
    }
)


def select_most_recent(datapoints: list[dict[str, Any]], statistic: str) -> tuple[float, str]:
    """Pick the latest datapoint and return its value and ISO timestamp.

    Given a non-empty list of CloudWatch datapoints (each a dict carrying a
    ``Timestamp`` and the requested statistic key), return a
    ``(value, iso_timestamp)`` tuple where ``value`` is the chosen
    datapoint's ``statistic`` reading coerced to a float and ``iso_timestamp``
    is its timestamp in ISO-8601 form. Selection is deterministic: the
    datapoint with the maximum ``Timestamp`` wins, and ``GetMetricStatistics``
    never emits two datapoints sharing a timestamp within one period.

    Args:
        datapoints: A non-empty list of CloudWatch datapoint dicts.
        statistic: The statistic key to read from the chosen datapoint
            (for example ``"Average"`` or ``"Sum"``).

    Returns:
        A ``(value, iso_timestamp)`` tuple for the latest datapoint.
    """
    most_recent = max(datapoints, key=lambda dp: dp["Timestamp"])
    value = float(most_recent[statistic])
    timestamp = most_recent["Timestamp"]
    iso_timestamp = timestamp.isoformat() if hasattr(timestamp, "isoformat") else str(timestamp)
    return value, iso_timestamp


def get_datapoint(
    *,
    metric_name: str,
    namespace: str,
    dimensions: dict[str, str] | None,
    region: str,
    period: int,
    statistic: str,
    start_time: datetime,
    end_time: datetime,
) -> tuple[float, str]:
    """Read one CloudWatch datapoint for a metric in a named region.

    Constructs a region-scoped CloudWatch client and issues a single
    read-only ``GetMetricStatistics`` request bounded to
    ``[start_time, end_time]`` for the supplied metric, namespace,
    dimensions, period, and statistic. The dimensions mapping is passed to
    CloudWatch unchanged, as a list of ``{"Name": ..., "Value": ...}`` pairs.

    Args:
        metric_name: The CloudWatch metric name.
        namespace: The CloudWatch namespace the metric lives in.
        dimensions: Name/value dimension pairs, or ``None`` for no dimensions.
        region: The AWS region to scope the CloudWatch client to.
        period: The aggregation period, in seconds.
        statistic: The statistic to request (for example ``"Average"``).
        start_time: The inclusive start of the lookback window.
        end_time: The end of the lookback window.

    Returns:
        A ``(value, iso_timestamp)`` tuple for the latest datapoint in the
        window, as produced by :func:`select_most_recent`.

    Raises:
        MetricReaderError: With :attr:`ErrorCode.NO_DATAPOINTS` when the
            window holds no datapoints, or :attr:`ErrorCode.AWS_UNREACHABLE`
            (carrying a ``kind`` discriminator of ``unreachable``,
            ``unauthorized``, or ``client_error``) when the request cannot be
            completed.
    """
    import boto3
    from botocore.exceptions import (
        BotoCoreError,
        ClientError,
        EndpointConnectionError,
    )

    dimension_pairs = [{"Name": key, "Value": value} for key, value in (dimensions or {}).items()]

    try:
        client = boto3.client("cloudwatch", region_name=region)
        response = client.get_metric_statistics(
            Namespace=namespace,
            MetricName=metric_name,
            Dimensions=dimension_pairs,
            StartTime=start_time,
            EndTime=end_time,
            Period=period,
            Statistics=[statistic],
        )
    except EndpointConnectionError as exc:
        # A subclass of BotoCoreError; caught first so it keeps its own,
        # more specific "unreachable" classification.
        raise MetricReaderError(
            ErrorCode.AWS_UNREACHABLE,
            {"kind": "unreachable", "region": region, "message": str(exc)},
        ) from exc
    except ClientError as exc:
        aws_error_code = exc.response.get("Error", {}).get("Code", "")
        kind = "unauthorized" if aws_error_code in _UNAUTHORIZED_ERROR_CODES else "client_error"
        raise MetricReaderError(
            ErrorCode.AWS_UNREACHABLE,
            {
                "kind": kind,
                "region": region,
                "aws_error_code": aws_error_code,
                "message": str(exc),
            },
        ) from exc
    except BotoCoreError as exc:
        raise MetricReaderError(
            ErrorCode.AWS_UNREACHABLE,
            {"kind": "unreachable", "region": region, "message": str(exc)},
        ) from exc

    datapoints = response.get("Datapoints", [])
    if not datapoints:
        raise MetricReaderError(
            ErrorCode.NO_DATAPOINTS,
            {
                "metric_name": metric_name,
                "namespace": namespace,
                "region": region,
                "statistic": statistic,
            },
        )

    return select_most_recent(datapoints, statistic)
