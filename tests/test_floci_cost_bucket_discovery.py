"""Floci layer: the SSM-published cost report bucket contract (ADR-0005).

The monitoring stack lets CloudFormation name the cost report bucket and
publishes ``/<project>/cost-report-bucket/{name,arn,region}`` in the
monitoring Region. Three consumers read that identity, and the unit suite
covers each of them over MagicMocks only:

* ``gco/services/cost_monitor.py`` — ``CostReportBucketLocator`` inside the
  regional cost-monitor service (lazy discovery, TTL refresh, stale fallback,
  the "not published yet" skip);
* ``scripts/live_release_validation/checks/opencost.py`` — release validation
  matching the API-reported bucket against the published name and heading
  the written object;
* ``cli/storage.py`` — ``gco storage`` bucket resolution.

Here the "monitoring stack" is a real CloudFormation stack in the emulator: an
unnamed ``AWS::S3::Bucket`` plus the three parameters, declared the way
``gco/stacks/monitoring_stack.py`` declares them and named through the same
``cost_report_ssm_parameter_prefix`` every consumer imports. What runs for
real is CloudFormation generating the physical name, SSM serving it across
Regions, and every consumer landing on the bucket the writer used — with the
genuine ``ParameterNotFound`` and S3 404 shapes on the negative paths, which
the unit tests can only fabricate.
"""

from __future__ import annotations

import json
import re
import threading
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from types import SimpleNamespace

import boto3
import pytest
from botocore.exceptions import WaiterError

from tests._floci import floci_test_markers, unique_name

pytestmark = floci_test_markers()

#: The topology under test: the cost-monitor service runs in a deployment
#: Region and reads the parameter from the monitoring Region, exactly the
#: cross-Region rendezvous the regional stack grants ``ssm:GetParameter`` for.
MONITORING_REGION = "us-east-2"
SERVICE_REGION = "us-east-1"

_MANIFEST = (
    Path(__file__).resolve().parents[1]
    / "lambda"
    / "kubectl-applier-simple"
    / "manifests"
    / "34-cost-monitor.yaml"
)


def _project_name() -> str:
    return unique_name("gcotest").replace("-", "")[:16]


def _monitoring_template(project: str) -> str:
    """The monitoring stack's bucket-identity resources, as CDK declares them.

    No ``BucketName`` on the bucket (CloudFormation names it) and the three
    parameters derived from the same prefix helper production reads.
    """
    from gco.stacks.constants import cost_report_ssm_parameter_prefix

    prefix = cost_report_ssm_parameter_prefix(project)
    return json.dumps(
        {
            "AWSTemplateFormatVersion": "2010-09-09",
            "Resources": {
                "CostReportBucketA1B2C3D4": {
                    "Type": "AWS::S3::Bucket",
                    "Properties": {},
                },
                "CostReportBucketNameParam": {
                    "Type": "AWS::SSM::Parameter",
                    "Properties": {
                        "Name": f"{prefix}/name",
                        "Type": "String",
                        "Value": {"Ref": "CostReportBucketA1B2C3D4"},
                    },
                },
                "CostReportBucketArnParam": {
                    "Type": "AWS::SSM::Parameter",
                    "Properties": {
                        "Name": f"{prefix}/arn",
                        "Type": "String",
                        "Value": {"Fn::GetAtt": ["CostReportBucketA1B2C3D4", "Arn"]},
                    },
                },
                "CostReportBucketRegionParam": {
                    "Type": "AWS::SSM::Parameter",
                    "Properties": {
                        "Name": f"{prefix}/region",
                        "Type": "String",
                        "Value": {"Ref": "AWS::Region"},
                    },
                },
            },
        }
    )


class _MonitoringStack:
    """One emulated monitoring stack: created on demand, torn down completely."""

    def __init__(self, project: str) -> None:
        self.project = project
        self.stack_name = f"{project}-monitoring"
        self._cloudformation = boto3.client("cloudformation", region_name=MONITORING_REGION)
        self._s3 = boto3.client("s3", region_name=MONITORING_REGION)
        self.bucket: str | None = None
        self.destroyed = False

    def create(self) -> str:
        """Deploy the stack and return the bucket name CloudFormation chose."""
        self._cloudformation.create_stack(
            StackName=self.stack_name, TemplateBody=_monitoring_template(self.project)
        )
        self._cloudformation.get_waiter("stack_create_complete").wait(
            StackName=self.stack_name, WaiterConfig={"Delay": 1, "MaxAttempts": 120}
        )
        buckets = [
            resource["PhysicalResourceId"]
            for resource in self._cloudformation.list_stack_resources(StackName=self.stack_name)[
                "StackResourceSummaries"
            ]
            if resource["ResourceType"] == "AWS::S3::Bucket"
        ]
        assert len(buckets) == 1, buckets
        self.bucket = buckets[0]
        # The point of ADR-0005: the physical name is CloudFormation's, not a
        # formula anyone could reconstruct (or collide with) from the account.
        assert self.bucket.startswith(self.stack_name.lower()), self.bucket
        assert self.project in self.bucket
        return self.bucket

    def object_keys(self) -> list[str]:
        assert self.bucket is not None
        paginator = self._s3.get_paginator("list_objects_v2")
        return [
            item["Key"]
            for page in paginator.paginate(Bucket=self.bucket)
            for item in page.get("Contents", [])
        ]

    def destroy(self) -> None:
        if self.bucket is not None:
            for key in self.object_keys():
                self._s3.delete_object(Bucket=self.bucket, Key=key)
        self._cloudformation.delete_stack(StackName=self.stack_name)
        try:
            self._cloudformation.get_waiter("stack_delete_complete").wait(
                StackName=self.stack_name, WaiterConfig={"Delay": 1, "MaxAttempts": 120}
            )
        except WaiterError as exc:
            events = self._cloudformation.describe_stack_events(StackName=self.stack_name)
            failures = [
                (
                    event["LogicalResourceId"],
                    event["ResourceStatus"],
                    event.get("ResourceStatusReason"),
                )
                for event in events["StackEvents"]
                if event["ResourceStatus"].endswith("FAILED")
            ]
            raise AssertionError(f"{self.stack_name} did not delete cleanly: {failures}") from exc
        self.bucket = None
        self.destroyed = True


@pytest.fixture()
def monitoring_stacks(verified_floci_endpoint):
    """Factory for monitoring stacks; every stack it made is destroyed after the test."""
    created: list[_MonitoringStack] = []

    def _factory(project: str) -> _MonitoringStack:
        stack = _MonitoringStack(project)
        created.append(stack)
        stack.create()
        return stack

    yield _factory
    for stack in created:
        if not stack.destroyed:
            stack.destroy()


class _OpenCostStub(BaseHTTPRequestHandler):
    """Minimal OpenCost allocation API: one namespace with a fixed cost."""

    payload = {
        "code": 200,
        "data": [{"gco-jobs": {"name": "gco-jobs", "cpuCost": 1.5, "totalCost": 1.5}}],
    }

    def do_GET(self):  # noqa: N802 - http.server API
        body = json.dumps(self.payload).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        return


@pytest.fixture()
def opencost_stub():
    server = HTTPServer(("127.0.0.1", 0), _OpenCostStub)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_port}"
    server.shutdown()
    thread.join(timeout=5)


def _parameter_name(project: str) -> str:
    from gco.stacks.constants import cost_report_ssm_parameter_prefix

    return f"{cost_report_ssm_parameter_prefix(project)}/name"


def _discovering_monitor(project: str, opencost_url: str, *, refresh_seconds: float = 900.0):
    """A regional cost-monitor wired the way the manifest wires it."""
    from gco.services.cost_monitor import CostMonitor, CostReportBucketLocator, OpenCostClient

    return CostMonitor(
        region=SERVICE_REGION,
        cluster=f"gco-{SERVICE_REGION}",
        bucket_locator=CostReportBucketLocator(
            _parameter_name(project), MONITORING_REGION, refresh_seconds=refresh_seconds
        ),
        opencost=OpenCostClient(opencost_url),
    )


class TestCostMonitorDiscoversThePublishedBucket:
    def test_unpublished_bucket_skips_the_scheduled_pass_with_a_reason(
        self, verified_floci_endpoint, opencost_stub
    ):
        """Before the monitoring stack exists the service waits instead of crashing.

        Regional stacks deploy before the monitoring stack, so the first
        scheduled passes run against a parameter SSM genuinely does not have.
        The real ``ParameterNotFound`` must surface as a skip with a reason in
        ``status()``, never as an exception out of the scheduler loop — and
        never as a report written to a guessed bucket.
        """
        from gco.services.cost_monitor import CostReportBucketUnavailableError

        project = _project_name()
        monitor = _discovering_monitor(project, opencost_stub)

        assert monitor.run_scheduled_once(now=datetime(2026, 9, 12, 12, 34, tzinfo=UTC)) is None
        assert monitor.last_error is not None
        assert _parameter_name(project) in monitor.last_error
        assert "ParameterNotFound" in monitor.last_error, (
            "the reason must carry SSM's real error code so an operator reading "
            "/api/v1/cost/status can tell 'not published yet' from a permissions problem"
        )
        status = monitor.status()
        assert status["bucket"] is None
        assert status["bucket_source"] == "ssm"
        assert status["bucket_parameter"] == _parameter_name(project)

        with pytest.raises(CostReportBucketUnavailableError):
            monitor.generate_report(
                datetime(2026, 9, 12, 11, 0, tzinfo=UTC),
                datetime(2026, 9, 12, 12, 0, tzinfo=UTC),
                adhoc=True,
            )

    def test_discovery_writes_into_the_bucket_cloudformation_named(
        self, monitoring_stacks, opencost_stub
    ):
        project = _project_name()
        stack = monitoring_stacks(project)
        monitor = _discovering_monitor(project, opencost_stub)
        moment = datetime(2026, 9, 12, 12, 34, tzinfo=UTC)

        result = monitor.run_scheduled_once(now=moment)

        assert result is not None, "with the parameter published the pass must write"
        # Independent source of truth: the object landed in the bucket
        # CloudFormation created, not in anything derived from the account.
        assert stack.object_keys() == [result.s3_key]
        assert monitor.bucket == stack.bucket
        assert monitor.status()["bucket"] == stack.bucket
        assert monitor.last_error is None
        assert [report["key"] for report in monitor.list_reports()] == [result.s3_key]
        assert monitor.run_scheduled_once(now=moment) is None, (
            "the second pass must see the object through a real head_object and skip"
        )

    def test_environment_contract_matches_the_manifest_and_discovers(
        self, monitoring_stacks, opencost_stub, monkeypatch
    ):
        """The Deployment env the manifest renders builds a discovering monitor.

        The manifest and ``create_cost_monitor_from_env`` are two files that
        must agree on variable names; this pins the names to the manifest and
        then proves the resulting monitor writes to the published bucket.
        """
        from gco.services.cost_monitor import create_cost_monitor_from_env

        manifest_env = set(re.findall(r"^\s+- name: ([A-Z_]+)$", _MANIFEST.read_text(), re.M))
        assert {
            "REGION",
            "CLUSTER_NAME",
            "COST_REPORT_BUCKET_PARAMETER",
            "COST_REPORT_BUCKET_PARAMETER_REGION",
        } <= manifest_env, manifest_env
        assert "COST_REPORT_BUCKET" not in manifest_env, (
            "the manifest must not carry a reconstructed bucket name (ADR-0005)"
        )

        project = _project_name()
        stack = monitoring_stacks(project)
        monkeypatch.delenv("COST_REPORT_BUCKET", raising=False)
        monkeypatch.setenv("REGION", SERVICE_REGION)
        monkeypatch.setenv("CLUSTER_NAME", f"gco-{SERVICE_REGION}")
        monkeypatch.setenv("COST_REPORT_BUCKET_PARAMETER", _parameter_name(project))
        monkeypatch.setenv("COST_REPORT_BUCKET_PARAMETER_REGION", MONITORING_REGION)
        monkeypatch.setenv("OPENCOST_BASE_URL", opencost_stub)

        monitor = create_cost_monitor_from_env()

        assert monitor.bucket is None, "construction must not touch AWS"
        assert monitor.bucket_source == "ssm"
        result = monitor.run_scheduled_once(now=datetime(2026, 9, 12, 13, 5, tzinfo=UTC))
        assert result is not None
        assert stack.object_keys() == [result.s3_key]

    def test_refresh_follows_a_replaced_monitoring_stack_and_survives_its_absence(
        self, monitoring_stacks, opencost_stub
    ):
        """A re-created monitoring stack means a new bucket; discovery must follow.

        Replacing the stack is exactly the upgrade ADR-0005 warns about, and
        also what a destroy/deploy cycle does. With the cache expired the
        locator must return the new name; while the parameter is missing in
        between it must keep serving the last known bucket rather than fail a
        pass over a transient gap.
        """
        from gco.services.cost_monitor import CostReportBucketLocator

        project = _project_name()
        first = monitoring_stacks(project)
        first_bucket = first.bucket
        locator = CostReportBucketLocator(
            _parameter_name(project), MONITORING_REGION, refresh_seconds=0
        )
        assert locator.resolve() == first_bucket

        first.destroy()
        assert locator.resolve() == first_bucket, (
            "a refresh that hits a real ParameterNotFound must keep the cached bucket"
        )
        assert locator.cached == first_bucket

        second = monitoring_stacks(project)
        assert second.bucket != first_bucket, "CloudFormation must have chosen a fresh name"
        assert locator.resolve() == second.bucket

        monitor = _discovering_monitor(project, opencost_stub, refresh_seconds=0)
        result = monitor.run_scheduled_once(now=datetime(2026, 9, 12, 14, 0, tzinfo=UTC))
        assert result is not None
        assert second.object_keys() == [result.s3_key]


def _validation_context(project: str) -> SimpleNamespace:
    """The slice of ``RunContext`` the OpenCost check reads, over a real session."""
    return SimpleNamespace(
        session=boto3.Session(),
        cdk_context={"deployment_regions": {"monitoring": MONITORING_REGION}},
        config=SimpleNamespace(project_name=project, global_region=MONITORING_REGION),
        deployment_regions=(SERVICE_REGION,),
    )


class TestReleaseValidationReadsTheSameIdentity:
    def test_expected_bucket_is_the_published_one_and_the_report_verifies(
        self, monitoring_stacks, opencost_stub
    ):
        from scripts.live_release_validation.checks import opencost as check

        project = _project_name()
        stack = monitoring_stacks(project)
        ctx = _validation_context(project)

        assert check._expected_report_bucket(ctx) == stack.bucket

        # The report the regional service would return from POST /cost/reports.
        monitor = _discovering_monitor(project, opencost_stub)
        written = monitor.generate_report(
            datetime(2026, 9, 12, 10, 0, tzinfo=UTC),
            datetime(2026, 9, 12, 12, 0, tzinfo=UTC),
            adhoc=True,
        )
        report = {
            "region": SERVICE_REGION,
            "s3_key": written.s3_key,
            "row_count": written.row_count,
            "bucket": monitor.bucket,
        }

        verified = check._verify_report_object(ctx, report)

        assert verified["bucket"] == stack.bucket
        assert verified["key"] == written.s3_key
        assert verified["size_bytes"] > 0

        with pytest.raises(RuntimeError, match="unexpected bucket"):
            check._verify_report_object(ctx, {**report, "bucket": f"{stack.bucket}-impostor"})

    def test_unpublished_parameter_and_missing_object_fail_validation(
        self, monitoring_stacks, opencost_stub
    ):
        from scripts.live_release_validation.checks import opencost as check

        unpublished = _project_name()
        with pytest.raises(RuntimeError, match="not readable") as excinfo:
            check._expected_report_bucket(_validation_context(unpublished))
        assert _parameter_name(unpublished) in str(excinfo.value)
        assert "ParameterNotFound" in str(excinfo.value)

        project = _project_name()
        stack = monitoring_stacks(project)
        ctx = _validation_context(project)
        missing = {
            "region": SERVICE_REGION,
            "s3_key": (
                f"adhoc/region={SERVICE_REGION}/date=2026-09-12/"
                "allocation-20260912T100000Z-20260912T120000Z-0badc0de.parquet"
            ),
            "row_count": 1,
            "bucket": stack.bucket,
        }
        with pytest.raises(RuntimeError, match="is not readable"):
            check._verify_report_object(ctx, missing)


class TestStorageCliResolvesTheSameIdentity:
    def test_cost_bucket_resolves_from_the_published_name_and_arn(self, monitoring_stacks):
        from cli.storage import BUCKET_DESCRIPTORS, StorageManager

        project = _project_name()
        stack = monitoring_stacks(project)
        manager = object.__new__(StorageManager)
        manager.config = SimpleNamespace(project_name=project)
        manager._stack_resource_cache = {}
        cost = next(item for item in BUCKET_DESCRIPTORS if item.id == "cost-reports")

        name, arn = manager._resolve_primary_bucket(cost, MONITORING_REGION, account=None)

        assert name == stack.bucket
        assert arn == f"arn:aws:s3:::{stack.bucket}", (
            "the ARN parameter is CloudFormation's GetAtt of the same bucket"
        )

    def test_unpublished_cost_bucket_reads_as_not_deployed(self, verified_floci_endpoint):
        from cli.storage import BUCKET_DESCRIPTORS, StorageManager

        manager = object.__new__(StorageManager)
        manager.config = SimpleNamespace(project_name=_project_name())
        manager._stack_resource_cache = {}
        cost = next(item for item in BUCKET_DESCRIPTORS if item.id == "cost-reports")

        assert manager._resolve_primary_bucket(cost, MONITORING_REGION, "123456789012") == (
            None,
            None,
        )
