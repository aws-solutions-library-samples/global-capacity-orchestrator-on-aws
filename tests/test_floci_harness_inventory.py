"""Floci layer: the live-validation harness's inventory machinery for real.

``scripts/live_release_validation`` decides what it may destroy from what its
inventory scanners observe, and ``final-inventory`` passes only when the
account is provably clean. The unit suite drives this with patched clients;
here the same functions run against emulator state through the harness's own
``ThrottleResilientSession``: region discovery uses the genuine
partition/available-region cross-check, the fail-closed scanner coverage
executes every service scanner over the wire, and baseline capture and
comparison fingerprint real CloudFormation stacks. This is the foundation the
Floci E2E (test_floci_live_validation_e2e.py) builds on — each piece is
proven here in isolation first, so an E2E failure points at orchestration,
not at scanners.
"""

from __future__ import annotations

import json

import boto3
import pytest

from tests._floci import floci_test_markers, unique_name

pytestmark = floci_test_markers()


@pytest.fixture(scope="module")
def harness_session(verified_floci_endpoint):
    from scripts.live_release_validation.aws_session import ThrottleResilientSession
    from tests._floci import apply_known_floci_gap_shims

    session = ThrottleResilientSession()
    # Two documented Floci 1.6.0 gaps get local answers (unparseable
    # GetStackPolicy responses; Global Accelerator absent entirely) so the
    # other twelve scanners run against the emulator for real. See
    # tests/_floci.py for the per-gap rationale.
    apply_known_floci_gap_shims(session.events)
    return session


def _deploy_marker_stack(region: str, stack_name: str) -> None:
    cloudformation = boto3.client("cloudformation", region_name=region)
    cloudformation.create_stack(
        StackName=stack_name,
        TemplateBody=json.dumps(
            {
                "AWSTemplateFormatVersion": "2010-09-09",
                "Resources": {
                    "Marker": {
                        "Type": "AWS::SSM::Parameter",
                        "Properties": {
                            "Name": f"/{stack_name}/marker",
                            "Type": "String",
                            "Value": "present",
                        },
                    }
                },
            }
        ),
    )
    cloudformation.get_waiter("stack_create_complete").wait(StackName=stack_name)


def _delete_stack(region: str, stack_name: str) -> None:
    cloudformation = boto3.client("cloudformation", region_name=region)
    cloudformation.delete_stack(StackName=stack_name)
    cloudformation.get_waiter("stack_delete_complete").wait(StackName=stack_name)


class TestRegionDiscovery:
    def test_enabled_regions_cross_check_partition_and_ec2(self, harness_session):
        from scripts.live_release_validation.inventory.stacks import discover_enabled_regions

        regions = discover_enabled_regions(harness_session, "us-east-1")
        assert "us-east-1" in regions and "us-east-2" in regions, (
            "the emulator's describe_regions must intersect botocore's partition data "
            f"to a usable region list, got {regions}"
        )


class TestProjectResourceScanners:
    def test_created_resources_are_seen_and_absence_is_proven_after_cleanup(
        self, harness_session, floci_account
    ):
        from scripts.live_release_validation.inventory.project import (
            collect_project_resources,
            project_resources_are_absent,
            summarize_project_resources,
        )

        project = unique_name("gcoscan").replace("-", "")[:14]
        region = "us-east-1"
        sqs = boto3.client("sqs", region_name=region)
        dynamodb = boto3.client("dynamodb", region_name=region)
        queue_url = sqs.create_queue(QueueName=f"{project}-jobs")["QueueUrl"]
        dynamodb.create_table(
            TableName=f"{project}-jobs",
            AttributeDefinitions=[{"AttributeName": "job_id", "AttributeType": "S"}],
            KeySchema=[{"AttributeName": "job_id", "KeyType": "HASH"}],
            BillingMode="PAY_PER_REQUEST",
        )
        dynamodb.get_waiter("table_exists").wait(TableName=f"{project}-jobs")
        _deploy_marker_stack(region, f"{project}-{region}")

        inventory = collect_project_resources(
            harness_session,
            enabled_regions=[region],
            expected_account=floci_account,
            project_name=project,
            seed_region=region,
        )
        summary = summarize_project_resources(inventory)
        assert summary["sqs_queues"] >= 1, f"scanner missed the project queue: {summary}"
        assert summary["dynamodb_tables"] >= 1, f"scanner missed the project table: {summary}"
        assert summary["cloudformation_stacks"] >= 1, f"scanner missed the stack: {summary}"
        assert project_resources_are_absent(inventory) is False

        # Tear the project down and require the scanners to PROVE absence —
        # the exact final-inventory gate that authorizes calling a validation
        # run clean.
        sqs.delete_queue(QueueUrl=queue_url)
        dynamodb.delete_table(TableName=f"{project}-jobs")
        dynamodb.get_waiter("table_not_exists").wait(TableName=f"{project}-jobs")
        _delete_stack(region, f"{project}-{region}")

        after = collect_project_resources(
            harness_session,
            enabled_regions=[region],
            expected_account=floci_account,
            project_name=project,
            seed_region=region,
        )
        assert project_resources_are_absent(after) is True, (
            "with every project resource deleted, the fail-closed inventory must report "
            f"a complete all-zero scan; got {summarize_project_resources(after)} with "
            f"coverage={after.get('coverage')}"
        )


class TestProtectedBaseline:
    def test_baseline_is_stable_and_detects_protected_stack_loss(self, harness_session):
        from scripts.live_release_validation.inventory.project import (
            capture_baseline,
            compare_baseline,
        )

        region = "us-east-1"
        protected_name = unique_name("gcoprotected").replace("-", "")[:20]
        _deploy_marker_stack(region, protected_name)
        try:
            baseline = capture_baseline(
                harness_session,
                enabled_regions=[region],
                ecr_regions=[],
                protected_stack_names=[protected_name],
            )
            assert baseline["protected_stacks"].get(region), (
                "the protected stack must be fingerprinted into the baseline"
            )

            unchanged = capture_baseline(
                harness_session,
                enabled_regions=[region],
                ecr_regions=[],
                protected_stack_names=[protected_name],
            )
            assert compare_baseline(baseline, unchanged) == [], (
                "an untouched account must produce a difference-free baseline comparison"
            )
        finally:
            _delete_stack(region, protected_name)

        after_loss = capture_baseline(
            harness_session,
            enabled_regions=[region],
            ecr_regions=[],
            protected_stack_names=[protected_name],
        )
        differences = compare_baseline(baseline, after_loss)
        assert differences, (
            "losing a protected stack MUST surface as a baseline difference — this is "
            "the guarantee that a validation run cannot silently damage pre-existing "
            "infrastructure"
        )
        assert differences[0]["category"] == "protected_stacks"
