"""
Tests for gco/services/cost_monitor.py — the cost-monitor service core.

Covers the OpenCost allocation client (transport failures, non-200s,
malformed bodies), allocation-row normalization, real Parquet
serialization via pyarrow, deterministic scheduled report keys, aligned
window math, the CostMonitor orchestrator (generate/skip/list/status), and
the environment factory. S3 and httpx are mocked; pyarrow runs for real so
the Parquet contract with the Glue table is exercised, not simulated.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock, patch

import httpx
import pytest

from gco.services.cost_monitor import (
    ADHOC_PREFIX,
    ALLOCATION_REPORT_FIELDS,
    SCHEDULED_PREFIX,
    CostMonitor,
    OpenCostClient,
    OpenCostUnavailableError,
    ReportWriteError,
    adhoc_report_key,
    aligned_window,
    allocations_to_rows,
    create_cost_monitor_from_env,
    rows_to_parquet_bytes,
    scheduled_report_key,
)

WINDOW_START = datetime(2026, 7, 26, 9, 0, tzinfo=UTC)
WINDOW_END = datetime(2026, 7, 26, 10, 0, tzinfo=UTC)


def _allocation(total: float = 1.5, **overrides) -> dict:
    base = {
        "cpuCoreHours": 2.0,
        "cpuCost": 0.5,
        "ramByteHours": float(4 * 1024**3),
        "ramCost": 0.25,
        "gpuHours": 1.0,
        "gpuCost": 0.6,
        "pvCost": 0.05,
        "networkCost": 0.02,
        "loadBalancerCost": 0.03,
        "sharedCost": 0.0,
        "externalCost": 0.0,
        "totalCost": total,
        "totalEfficiency": 0.8,
    }
    base.update(overrides)
    return base


def _monitor(**kwargs) -> CostMonitor:
    defaults = {
        "region": "us-east-1",
        "cluster": "gco-us-east-1",
        "bucket": "gco-cost-reports-123456789012-us-east-2",
        "opencost": MagicMock(spec=OpenCostClient),
        "s3_client": MagicMock(),
    }
    defaults.update(kwargs)
    return CostMonitor(**defaults)


class TestOpenCostClient:
    def test_healthz_true_on_200(self):
        client = OpenCostClient("http://opencost:9003")
        with patch("gco.services.cost_monitor.httpx.get") as mock_get:
            mock_get.return_value = MagicMock(status_code=200)
            assert client.is_healthy() is True
        mock_get.assert_called_once()
        assert mock_get.call_args.args[0] == "http://opencost:9003/healthz"

    def test_healthz_false_on_non_200(self):
        client = OpenCostClient("http://opencost:9003")
        with patch("gco.services.cost_monitor.httpx.get") as mock_get:
            mock_get.return_value = MagicMock(status_code=503)
            assert client.is_healthy() is False

    def test_healthz_false_on_transport_error(self):
        client = OpenCostClient("http://opencost:9003")
        with patch("gco.services.cost_monitor.httpx.get", side_effect=httpx.ConnectError("no")):
            assert client.is_healthy() is False

    def test_get_allocation_merges_allocation_sets(self):
        client = OpenCostClient("http://opencost:9003/")
        payload = {
            "code": 200,
            "data": [
                {"gco-jobs": _allocation(2.0), "monitoring": _allocation(1.0)},
                {"__idle__": _allocation(0.5)},
                "not-a-dict",
            ],
        }
        response = MagicMock(status_code=200)
        response.json.return_value = payload
        with patch("gco.services.cost_monitor.httpx.get", return_value=response) as mock_get:
            merged = client.get_allocation(WINDOW_START, WINDOW_END)
        assert set(merged) == {"gco-jobs", "monitoring", "__idle__"}
        params = mock_get.call_args.kwargs["params"]
        assert params["window"] == "2026-07-26T09:00:00Z,2026-07-26T10:00:00Z"
        assert params["aggregate"] == "namespace"
        assert params["accumulate"] == "true"

    def test_get_allocation_raises_on_transport_error(self):
        client = OpenCostClient("http://opencost:9003")
        with (
            patch("gco.services.cost_monitor.httpx.get", side_effect=httpx.ReadTimeout("slow")),
            pytest.raises(OpenCostUnavailableError, match="request failed"),
        ):
            client.get_allocation(WINDOW_START, WINDOW_END)

    def test_get_allocation_raises_on_http_error(self):
        client = OpenCostClient("http://opencost:9003")
        response = MagicMock(status_code=500)
        with (
            patch("gco.services.cost_monitor.httpx.get", return_value=response),
            pytest.raises(OpenCostUnavailableError, match="HTTP 500"),
        ):
            client.get_allocation(WINDOW_START, WINDOW_END)

    def test_get_allocation_raises_on_non_json(self):
        client = OpenCostClient("http://opencost:9003")
        response = MagicMock(status_code=200)
        response.json.side_effect = ValueError("not json")
        with (
            patch("gco.services.cost_monitor.httpx.get", return_value=response),
            pytest.raises(OpenCostUnavailableError, match="non-JSON"),
        ):
            client.get_allocation(WINDOW_START, WINDOW_END)

    def test_get_allocation_raises_on_missing_data(self):
        client = OpenCostClient("http://opencost:9003")
        response = MagicMock(status_code=200)
        response.json.return_value = {"code": 200}
        with (
            patch("gco.services.cost_monitor.httpx.get", return_value=response),
            pytest.raises(OpenCostUnavailableError, match="omitted data"),
        ):
            client.get_allocation(WINDOW_START, WINDOW_END)


class TestAllocationsToRows:
    def test_rows_carry_every_report_field(self):
        rows = allocations_to_rows(
            {"gco-jobs": _allocation()},
            cluster="gco-us-east-1",
            window_start=WINDOW_START,
            window_end=WINDOW_END,
        )
        assert len(rows) == 1
        assert set(rows[0]) == set(ALLOCATION_REPORT_FIELDS)
        assert rows[0]["namespace"] == "gco-jobs"
        assert rows[0]["cluster"] == "gco-us-east-1"
        assert rows[0]["ram_gib_hours"] == pytest.approx(4.0)
        assert rows[0]["window_start"] == "2026-07-26T09:00:00+00:00"

    def test_rows_sorted_by_descending_total_cost(self):
        rows = allocations_to_rows(
            {
                "cheap": _allocation(0.1),
                "expensive": _allocation(9.0),
                "middle": _allocation(1.0),
            },
            cluster="c",
            window_start=WINDOW_START,
            window_end=WINDOW_END,
        )
        assert [row["namespace"] for row in rows] == ["expensive", "middle", "cheap"]

    def test_malformed_numerics_coerce_to_zero(self):
        rows = allocations_to_rows(
            {"weird": _allocation(totalCost="not-a-number", cpuCost=None, gpuCost=float("nan"))},
            cluster="c",
            window_start=WINDOW_START,
            window_end=WINDOW_END,
        )
        assert rows[0]["total_cost"] == 0.0
        assert rows[0]["cpu_cost"] == 0.0
        assert rows[0]["gpu_cost"] == 0.0

    def test_infinite_numerics_coerce_to_zero(self):
        """json.loads accepts Infinity/-Infinity; either would poison Athena SUMs."""
        rows = allocations_to_rows(
            {"inf": _allocation(totalCost=float("inf"), cpuCost=float("-inf"), gpuCost="inf")},
            cluster="c",
            window_start=WINDOW_START,
            window_end=WINDOW_END,
        )
        assert rows[0]["total_cost"] == 0.0
        assert rows[0]["cpu_cost"] == 0.0
        assert rows[0]["gpu_cost"] == 0.0


class TestParquetSerialization:
    def test_round_trips_through_real_pyarrow(self):
        import io

        import pyarrow.parquet as pq

        rows = allocations_to_rows(
            {"gco-jobs": _allocation(2.5), "__idle__": _allocation(0.5)},
            cluster="gco-us-east-1",
            window_start=WINDOW_START,
            window_end=WINDOW_END,
        )
        payload = rows_to_parquet_bytes(rows)
        table = pq.read_table(io.BytesIO(payload))
        assert table.num_rows == 2
        assert table.column_names == list(ALLOCATION_REPORT_FIELDS)
        namespaces = table.column("namespace").to_pylist()
        assert namespaces == ["gco-jobs", "__idle__"]
        window_starts = table.column("window_start").to_pylist()
        assert window_starts[0] == WINDOW_START

    def test_empty_rows_still_produce_a_valid_file(self):
        import io

        import pyarrow.parquet as pq

        payload = rows_to_parquet_bytes([])
        table = pq.read_table(io.BytesIO(payload))
        assert table.num_rows == 0
        assert table.column_names == list(ALLOCATION_REPORT_FIELDS)


class TestReportKeys:
    def test_scheduled_key_is_deterministic_and_partitioned(self):
        key_a = scheduled_report_key("us-east-1", WINDOW_START, WINDOW_END)
        key_b = scheduled_report_key("us-east-1", WINDOW_START, WINDOW_END)
        assert key_a == key_b
        assert key_a == (
            f"{SCHEDULED_PREFIX}/region=us-east-1/date=2026-07-26/"
            "allocation-20260726T090000Z-20260726T100000Z.parquet"
        )

    def test_adhoc_keys_are_unique_and_separately_prefixed(self):
        key_a = adhoc_report_key("us-east-1", WINDOW_START, WINDOW_END)
        key_b = adhoc_report_key("us-east-1", WINDOW_START, WINDOW_END)
        assert key_a != key_b
        assert key_a.startswith(f"{ADHOC_PREFIX}/region=us-east-1/date=")
        assert key_a.endswith(".parquet")

    def test_prefixes_stay_in_lockstep_with_stack_constants(self):
        from gco.stacks.constants import (
            COST_REPORT_ADHOC_PREFIX,
            COST_REPORT_SCHEDULED_PREFIX,
        )

        assert SCHEDULED_PREFIX == COST_REPORT_SCHEDULED_PREFIX
        assert ADHOC_PREFIX == COST_REPORT_ADHOC_PREFIX


class TestAlignedWindow:
    def test_returns_most_recent_completed_hour(self):
        now = datetime(2026, 7, 26, 10, 25, 13, tzinfo=UTC)
        start, end = aligned_window(now, 60)
        assert start == datetime(2026, 7, 26, 9, 0, tzinfo=UTC)
        assert end == datetime(2026, 7, 26, 10, 0, tzinfo=UTC)

    def test_exact_boundary_returns_previous_window(self):
        now = datetime(2026, 7, 26, 10, 0, tzinfo=UTC)
        start, end = aligned_window(now, 60)
        assert start == datetime(2026, 7, 26, 9, 0, tzinfo=UTC)
        assert end == datetime(2026, 7, 26, 10, 0, tzinfo=UTC)

    def test_sub_hour_intervals_align(self):
        now = datetime(2026, 7, 26, 10, 25, tzinfo=UTC)
        start, end = aligned_window(now, 15)
        assert start == datetime(2026, 7, 26, 10, 0, tzinfo=UTC)
        assert end == datetime(2026, 7, 26, 10, 15, tzinfo=UTC)


class TestCostMonitorGenerateReport:
    def test_writes_parquet_to_scheduled_key(self):
        monitor = _monitor()
        monitor.opencost.get_allocation.return_value = {"gco-jobs": _allocation(2.0)}

        result = monitor.generate_report(WINDOW_START, WINDOW_END, adhoc=False)

        put_call = monitor._s3.put_object.call_args
        assert put_call.kwargs["Bucket"] == monitor.bucket
        assert put_call.kwargs["Key"] == scheduled_report_key("us-east-1", WINDOW_START, WINDOW_END)
        assert result.row_count == 1
        assert result.total_cost == pytest.approx(2.0)
        assert result.rows == []

    def test_adhoc_report_lands_under_adhoc_prefix_with_rows(self):
        monitor = _monitor()
        monitor.opencost.get_allocation.return_value = {"gco-jobs": _allocation(2.0)}

        result = monitor.generate_report(WINDOW_START, WINDOW_END, adhoc=True, include_rows=True)

        assert monitor._s3.put_object.call_args.kwargs["Key"].startswith(f"{ADHOC_PREFIX}/")
        assert len(result.rows) == 1
        assert result.summary()["row_count"] == 1
        assert "rows" not in result.summary()

    def test_rejects_inverted_and_extreme_windows(self):
        monitor = _monitor()
        with pytest.raises(ValueError, match="after"):
            monitor.generate_report(WINDOW_END, WINDOW_START, adhoc=True)
        with pytest.raises(ValueError, match="capped"):
            monitor.generate_report(WINDOW_START - timedelta(days=10), WINDOW_END, adhoc=True)
        with pytest.raises(ValueError, match="at least"):
            monitor.generate_report(WINDOW_START, WINDOW_START + timedelta(minutes=1), adhoc=True)

    def test_s3_failure_raises_report_write_error(self):
        monitor = _monitor()
        monitor.opencost.get_allocation.return_value = {"gco-jobs": _allocation()}
        monitor._s3.put_object.side_effect = RuntimeError("denied")
        with pytest.raises(ReportWriteError, match="Failed to write"):
            monitor.generate_report(WINDOW_START, WINDOW_END, adhoc=False)

    def test_opencost_failure_propagates(self):
        monitor = _monitor()
        monitor.opencost.get_allocation.side_effect = OpenCostUnavailableError("down")
        with pytest.raises(OpenCostUnavailableError):
            monitor.generate_report(WINDOW_START, WINDOW_END, adhoc=False)


class TestCostMonitorScheduledPass:
    def test_writes_when_window_object_absent(self):
        monitor = _monitor()
        monitor._s3.head_object.side_effect = RuntimeError("404")
        monitor.opencost.get_allocation.return_value = {"gco-jobs": _allocation(3.0)}

        result = monitor.run_scheduled_once(now=datetime(2026, 7, 26, 10, 30, tzinfo=UTC))

        assert result is not None
        assert monitor.last_scheduled_report == result.summary()
        assert monitor.last_error is None

    def test_skips_when_window_object_already_exists(self):
        monitor = _monitor()
        monitor._s3.head_object.return_value = {"ContentLength": 10}

        result = monitor.run_scheduled_once(now=datetime(2026, 7, 26, 10, 30, tzinfo=UTC))

        assert result is None
        monitor._s3.put_object.assert_not_called()

    def test_failure_records_last_error_and_reraises(self):
        monitor = _monitor()
        monitor._s3.head_object.side_effect = RuntimeError("404")
        monitor.opencost.get_allocation.side_effect = OpenCostUnavailableError("down")

        with pytest.raises(OpenCostUnavailableError):
            monitor.run_scheduled_once(now=datetime(2026, 7, 26, 10, 30, tzinfo=UTC))
        assert monitor.last_error == "down"

    def test_interval_is_bounded(self):
        assert _monitor(report_interval_minutes=1).report_interval_minutes == 5
        assert _monitor(report_interval_minutes=100_000).report_interval_minutes == 1_440


class TestCostMonitorListReports:
    def test_lists_newest_first_with_bounded_limit(self):
        monitor = _monitor()
        paginator = MagicMock()
        monitor._s3.get_paginator.return_value = paginator
        paginator.paginate.return_value = [
            {
                "Contents": [
                    {
                        "Key": "reports/region=us-east-1/date=2026-07-25/old.parquet",
                        "Size": 100,
                        "LastModified": datetime(2026, 7, 25, tzinfo=UTC),
                    },
                    {
                        "Key": "reports/region=us-east-1/date=2026-07-26/new.parquet",
                        "Size": 200,
                        "LastModified": datetime(2026, 7, 26, tzinfo=UTC),
                    },
                ]
            }
        ]

        reports = monitor.list_reports(limit=1)

        assert len(reports) == 1
        assert reports[0]["key"].endswith("new.parquet")
        prefix = paginator.paginate.call_args.kwargs["Prefix"]
        assert prefix == f"{SCHEDULED_PREFIX}/region=us-east-1/"

    def test_adhoc_listing_uses_adhoc_prefix(self):
        monitor = _monitor()
        paginator = MagicMock()
        monitor._s3.get_paginator.return_value = paginator
        paginator.paginate.return_value = [{}]

        assert monitor.list_reports(adhoc=True) == []
        prefix = paginator.paginate.call_args.kwargs["Prefix"]
        assert prefix == f"{ADHOC_PREFIX}/region=us-east-1/"


class TestCostMonitorStatus:
    def test_healthy_with_data(self):
        monitor = _monitor()
        monitor.opencost.is_healthy.return_value = True
        monitor.opencost.get_allocation.return_value = {"gco-jobs": _allocation()}

        status = monitor.status()

        assert status["opencost_healthy"] is True
        assert status["opencost_returning_data"] is True
        assert status["allocation_names"] == ["gco-jobs"]
        assert status["region"] == "us-east-1"

    def test_healthy_but_empty_reports_no_data(self):
        monitor = _monitor()
        monitor.opencost.is_healthy.return_value = True
        monitor.opencost.get_allocation.return_value = {}

        status = monitor.status()

        assert status["opencost_healthy"] is True
        assert status["opencost_returning_data"] is False

    def test_probe_failure_reports_no_data(self):
        monitor = _monitor()
        monitor.opencost.is_healthy.return_value = True
        monitor.opencost.get_allocation.side_effect = OpenCostUnavailableError("boom")

        status = monitor.status()

        assert status["opencost_returning_data"] is False

    def test_unhealthy_skips_the_allocation_probe(self):
        monitor = _monitor()
        monitor.opencost.is_healthy.return_value = False

        status = monitor.status()

        assert status["opencost_healthy"] is False
        monitor.opencost.get_allocation.assert_not_called()


class TestCreateFromEnv:
    def test_builds_from_environment(self, monkeypatch):
        monkeypatch.setenv("COST_REPORT_BUCKET", "bucket-x")
        monkeypatch.setenv("REGION", "us-west-2")
        monkeypatch.setenv("CLUSTER_NAME", "gco-us-west-2")
        monkeypatch.setenv("OPENCOST_BASE_URL", "http://opencost.monitoring.svc:9003")
        monkeypatch.setenv("COST_REPORT_INTERVAL_MINUTES", "30")
        with patch("gco.services.cost_monitor.boto3.client"):
            monitor = create_cost_monitor_from_env()
        assert monitor.bucket == "bucket-x"
        assert monitor.region == "us-west-2"
        assert monitor.cluster == "gco-us-west-2"
        assert monitor.report_interval_minutes == 30
        assert monitor.opencost.base_url == "http://opencost.monitoring.svc:9003"

    def test_requires_bucket_and_region(self, monkeypatch):
        monkeypatch.delenv("COST_REPORT_BUCKET", raising=False)
        with pytest.raises(RuntimeError, match="COST_REPORT_BUCKET"):
            create_cost_monitor_from_env()
        monkeypatch.setenv("COST_REPORT_BUCKET", "bucket-x")
        monkeypatch.delenv("REGION", raising=False)
        monkeypatch.delenv("AWS_REGION", raising=False)
        with pytest.raises(RuntimeError, match="REGION"):
            create_cost_monitor_from_env()

    def test_invalid_interval_falls_back_to_default(self, monkeypatch):
        monkeypatch.setenv("COST_REPORT_BUCKET", "bucket-x")
        monkeypatch.setenv("REGION", "us-west-2")
        monkeypatch.setenv("COST_REPORT_INTERVAL_MINUTES", "not-a-number")
        with patch("gco.services.cost_monitor.boto3.client"):
            monitor = create_cost_monitor_from_env()
        assert monitor.report_interval_minutes == 60
