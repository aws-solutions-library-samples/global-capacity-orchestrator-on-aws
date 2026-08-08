"""Floci layer: Secrets Manager auth tokens and the S3 cost-report pipeline.

Two production paths that unit tests cover only with in-process mocks:

* ``gco/services/auth_middleware.py`` loads HMAC tokens from Secrets Manager,
  parsing the region out of the secret ARN and caching rotation stages. Here
  the secret is a real wire-level resource: the ARN comes back from the
  emulator, the region-parsing runs on it, and rotation overlap
  (AWSCURRENT + AWSPENDING both valid) is driven through real
  ``put_secret_value`` staging.

* ``gco/services/cost_monitor.py`` writes Parquet allocation reports to S3
  and lists them back. The OpenCost side is a local HTTP stub (OpenCost is a
  Kubernetes deployment — out of scope for this layer); everything from
  ``rows_to_parquet_bytes`` through ``put_object``/``list_objects_v2``/
  ``head_object`` idempotency runs against the emulator for real.
"""

from __future__ import annotations

import json
import threading
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, HTTPServer

import boto3
import pytest

from tests._floci import floci_test_markers, unique_name

pytestmark = floci_test_markers()


@pytest.fixture()
def secretsmanager(verified_floci_endpoint: str):
    return boto3.client("secretsmanager")


@pytest.fixture()
def _clean_auth_middleware(monkeypatch):
    """Reset auth_middleware's module-level cache and client between tests."""
    import gco.services.auth_middleware as auth

    monkeypatch.setattr(auth, "_cached_tokens", set())
    monkeypatch.setattr(auth, "_token_expirations", {})
    monkeypatch.setattr(auth, "_cache_timestamp", 0.0)
    monkeypatch.setattr(auth, "_last_successful_refresh", 0.0)
    monkeypatch.setattr(auth, "_last_refresh_attempt", 0.0)
    # The lazy client must be rebuilt inside the session env so it signs for
    # the emulator, not whatever a previous test process state held.
    monkeypatch.setattr(auth, "_secrets_client", None)
    return auth


class TestAuthTokenLoading:
    def test_tokens_load_from_a_real_secret_arn(
        self, secretsmanager, monkeypatch, _clean_auth_middleware
    ):
        auth = _clean_auth_middleware
        arn = secretsmanager.create_secret(
            Name=unique_name("gco-auth"),
            SecretString=json.dumps({"token": "wire-token-1"}),
        )["ARN"]
        # The middleware derives its client region from the ARN's region
        # field — assert the emulator's ARN carries one before relying on it.
        assert arn.split(":")[3] == "us-east-1", arn

        monkeypatch.setenv("AUTH_SECRET_ARN", arn)
        tokens = auth.get_valid_tokens()
        assert tokens == {"wire-token-1"}, (
            "middleware must load the AWSCURRENT token from the emulator-backed secret"
        )

    def test_rotation_overlap_accepts_current_and_pending(
        self, secretsmanager, monkeypatch, _clean_auth_middleware
    ):
        auth = _clean_auth_middleware
        name = unique_name("gco-auth-rotating")
        arn = secretsmanager.create_secret(
            Name=name, SecretString=json.dumps({"token": "current-token"})
        )["ARN"]
        secretsmanager.put_secret_value(
            SecretId=arn,
            SecretString=json.dumps({"token": "pending-token"}),
            VersionStages=["AWSPENDING"],
        )

        monkeypatch.setenv("AUTH_SECRET_ARN", arn)
        tokens = auth.get_valid_tokens()
        assert "current-token" in tokens, "AWSCURRENT must always validate"
        assert "pending-token" in tokens, (
            "during rotation the AWSPENDING token must validate too, or every request "
            "signed with the new key 503s until rotation completes"
        )

    def test_missing_secret_yields_no_tokens_not_an_exception(
        self, secretsmanager, monkeypatch, _clean_auth_middleware
    ):
        auth = _clean_auth_middleware
        region = "us-east-1"
        account = boto3.client("sts").get_caller_identity()["Account"]
        monkeypatch.setenv(
            "AUTH_SECRET_ARN",
            f"arn:aws:secretsmanager:{region}:{account}:secret:does-not-exist",
        )
        assert auth.get_valid_tokens() == set(), (
            "an unfetchable secret must fail closed with an empty token set "
            "(the middleware then 503s) rather than raising into the request path"
        )


class _OpenCostStub(BaseHTTPRequestHandler):
    """Minimal OpenCost allocation API: one namespace, fixed costs."""

    payload = {
        "code": 200,
        "data": [
            {
                "gco-jobs": {
                    "name": "gco-jobs",
                    "cpuCost": 1.25,
                    "ramCost": 0.75,
                    "gpuCost": 4.0,
                    "totalCost": 6.0,
                }
            }
        ],
    }

    def do_GET(self):  # noqa: N802 - http.server API
        body = json.dumps(self.payload).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):  # silence per-request stderr noise
        return


@pytest.fixture()
def opencost_stub():
    server = HTTPServer(("127.0.0.1", 0), _OpenCostStub)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_port}"
    server.shutdown()
    thread.join(timeout=5)


class TestCostReportPipeline:
    @pytest.fixture()
    def monitor(self, verified_floci_endpoint, opencost_stub):
        from gco.services.cost_monitor import CostMonitor, OpenCostClient

        bucket = unique_name("gco-cost-reports")
        s3 = boto3.client("s3")
        s3.create_bucket(Bucket=bucket)
        monitor = CostMonitor(
            region="us-east-1",
            cluster="gco-us-east-1",
            bucket=bucket,
            opencost=OpenCostClient(opencost_stub),
        )
        yield monitor
        listed = s3.list_objects_v2(Bucket=bucket)
        for item in listed.get("Contents", []):
            s3.delete_object(Bucket=bucket, Key=item["Key"])
        s3.delete_bucket(Bucket=bucket)

    def test_report_generates_uploads_and_lists_back(self, monitor):
        window_end = datetime(2026, 8, 8, 12, 0, tzinfo=UTC)
        window_start = datetime(2026, 8, 8, 11, 0, tzinfo=UTC)
        result = monitor.generate_report(window_start, window_end, adhoc=True)
        assert result.row_count >= 1, "the stubbed allocation must produce at least one row"

        reports = monitor.list_reports(adhoc=True)
        assert any(report["key"] == result.s3_key for report in reports), (
            "the uploaded Parquet object must be discoverable through list_reports()"
        )

        # The object is a real Parquet file in the emulator, not a marker:
        # read it back and verify the allocation actually round-tripped.
        import io

        import pyarrow.parquet as pq

        body = (
            boto3.client("s3").get_object(Bucket=monitor.bucket, Key=result.s3_key)["Body"].read()
        )
        table = pq.read_table(io.BytesIO(body))
        namespaces = table.column("namespace").to_pylist()
        assert "gco-jobs" in namespaces

    def test_scheduled_run_is_idempotent_per_window(self, monitor):
        moment = datetime(2026, 8, 8, 12, 34, tzinfo=UTC)
        first = monitor.run_scheduled_once(now=moment)
        assert first is not None, "first scheduled pass for a window must write the report"
        second = monitor.run_scheduled_once(now=moment)
        assert second is None, (
            "a second pass over the same aligned window must detect the existing "
            "object via head_object and skip the write (idempotency contract)"
        )
