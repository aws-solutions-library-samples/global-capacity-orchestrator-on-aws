"""
Tests for S3 server access logging on the model weights bucket.

Synthesizes GCOGlobalStack against a minimal MockConfigLoader and
asserts against the CloudFormation template that both buckets exist
(model weights + dedicated access-logs bucket), that LoggingConfiguration
wires the first to the second, that the access-logs bucket carries a
lifecycle rule expiring objects at 90 days by default (and honors a
custom s3_access_logs.retention_days context override), and that
public access is blocked on both buckets. No Docker or AWS calls —
pure template assertions.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import aws_cdk as cdk
from aws_cdk import assertions

from gco.stacks.global_stack import GCOGlobalStack


# Re-use the mock ConfigLoader pattern from tests/test_cdk_stacks.py so this
# test file stays self-contained and doesn't require a real cdk.json.
class MockConfigLoader:
    """Minimal ConfigLoader stub sufficient for GCOGlobalStack synthesis."""

    def __init__(self, app=None):
        pass

    def get_project_name(self):
        return "gco-test"

    def get_regions(self):
        return ["us-east-1"]

    def get_global_region(self):
        return "us-east-2"

    def get_tags(self):
        return {"Environment": "test", "Project": "gco"}

    def get_global_accelerator_config(self):
        return {
            "name": "gco-test-accelerator",
            "health_check_grace_period": 30,
            "health_check_interval": 30,
            "health_check_timeout": 5,
            "health_check_path": "/api/v1/health",
        }

    def get_capacity_history_enabled(self):
        return False

    def get_capacity_history_config(self):
        return {
            "enabled": False,
            "retention_days": 90,
            "poll_interval_minutes": 15,
            "watch_instance_types": ["g5.xlarge", "p5.48xlarge"],
            "enabled_regions": [],
        }

    def get_mission_memory_enabled(self):
        return False

    def get_mission_memory_config(self):
        return {
            "enabled": False,
            "retention_days": 365,
            "dimensions": 1024,
            "distance_function": "COSINE",
            "top_k": 3,
        }


def _synth(app: cdk.App, construct_id: str = "test-global-stack") -> assertions.Template:
    stack = GCOGlobalStack(app, construct_id, config=MockConfigLoader(app))
    return assertions.Template.from_stack(stack)


def _find_model_weights_bucket(template: assertions.Template) -> Mapping[str, Any]:
    """Return the model weights bucket resource.

    Disambiguates from the always-on ``cluster_shared_bucket`` (also has
    ``LoggingConfiguration``) by filtering on the ``LogFilePrefix`` value
    ``model-bucket-logs/`` set by ``_create_model_bucket``. The cluster-shared
    bucket uses the prefix ``cluster-shared/``.
    """
    buckets = template.find_resources("AWS::S3::Bucket")
    matches = [
        b
        for b in buckets.values()
        if b.get("Properties", {}).get("LoggingConfiguration", {}).get("LogFilePrefix")
        == "model-bucket-logs/"
    ]
    assert len(matches) == 1, f"Expected exactly one model-weights bucket, found {len(matches)}"
    return matches[0]


def _find_access_logs_bucket(template: assertions.Template) -> Mapping[str, Any]:
    """Return the model-weights access-logs bucket resource.

    Disambiguates from the ``cluster_shared_access_logs_bucket`` (also has
    ``LifecycleConfiguration``) by locating the bucket referenced as the
    ``LoggingConfiguration.DestinationBucketName`` of the model-weights bucket.
    This is more robust than matching on SSE algorithm because both buckets now
    carry the same ``ExpireAccessLogs`` lifecycle rule shape.
    """
    model_bucket = _find_model_weights_bucket(template)
    dest_ref = model_bucket["Properties"]["LoggingConfiguration"]["DestinationBucketName"]
    assert isinstance(dest_ref, dict) and "Ref" in dest_ref, (
        f"model_bucket LoggingConfiguration.DestinationBucketName should be a Ref, got {dest_ref!r}"
    )
    buckets = template.find_resources("AWS::S3::Bucket")
    assert dest_ref["Ref"] in buckets, (
        f"model_bucket logs to {dest_ref['Ref']} but no such S3::Bucket exists"
    )
    return buckets[dest_ref["Ref"]]


class TestModelBucketAccessLogs:
    """S3 access-logging assertions for model artifact buckets.

    Verifies every synthesized model bucket has server access logging
    enabled and that logs are delivered to a dedicated access-log
    bucket. Catches regressions where a bucket ships without logging
    (blind spot during forensics / audit)."""

    def test_at_least_two_s3_buckets_exist(self):
        """Template has at least the model bucket and the access-logs bucket."""
        template = _synth(cdk.App())
        buckets = template.find_resources("AWS::S3::Bucket")
        assert len(buckets) >= 2, (
            f"Expected at least 2 S3 buckets (model + access logs), found {len(buckets)}"
        )

    def test_model_bucket_logs_to_access_logs_bucket(self):
        """Model weights bucket has LoggingConfiguration pointing at the access-logs bucket."""
        template = _synth(cdk.App())

        model_bucket = _find_model_weights_bucket(template)
        logging_cfg = model_bucket["Properties"]["LoggingConfiguration"]

        # Destination bucket must be a CFN reference to another S3::Bucket resource.
        dest = logging_cfg["DestinationBucketName"]
        assert isinstance(dest, dict) and "Ref" in dest, (
            f"LoggingConfiguration.DestinationBucketName should be a Ref, got {dest!r}"
        )

        buckets = template.find_resources("AWS::S3::Bucket")
        assert dest["Ref"] in buckets, (
            f"LoggingConfiguration references {dest['Ref']} but no such S3::Bucket exists"
        )

        # Verify the referenced bucket is the access-logs bucket (i.e. the one
        # with LifecycleConfiguration).
        access_logs_bucket_props = buckets[dest["Ref"]]["Properties"]
        assert "LifecycleConfiguration" in access_logs_bucket_props, (
            "LoggingConfiguration should point at the access-logs bucket "
            "(the one with a LifecycleConfiguration)"
        )

        # And the log prefix is set per the implementation.
        assert logging_cfg.get("LogFilePrefix") == "model-bucket-logs/"

    def test_access_logs_bucket_has_default_90_day_expiration(self):
        """Access-logs bucket has a lifecycle rule expiring objects at 90 days by default."""
        template = _synth(cdk.App())

        access_logs_bucket = _find_access_logs_bucket(template)
        rules = access_logs_bucket["Properties"]["LifecycleConfiguration"]["Rules"]

        expiring_rules = [
            r for r in rules if r.get("Status") == "Enabled" and "ExpirationInDays" in r
        ]
        assert expiring_rules, f"No enabled expiration rule found; rules={rules!r}"

        expiration_days = [r["ExpirationInDays"] for r in expiring_rules]
        assert 90 in expiration_days, f"Expected default 90-day expiration, got {expiration_days!r}"

    def test_access_logs_bucket_honors_custom_retention_context(self):
        """A custom s3_access_logs.retention_days context value propagates to the lifecycle rule."""
        app = cdk.App(context={"s3_access_logs": {"retention_days": 30}})
        template = _synth(app, construct_id="test-global-stack-custom-retention")

        access_logs_bucket = _find_access_logs_bucket(template)
        rules = access_logs_bucket["Properties"]["LifecycleConfiguration"]["Rules"]

        expiration_days = [
            r["ExpirationInDays"]
            for r in rules
            if r.get("Status") == "Enabled" and "ExpirationInDays" in r
        ]
        assert 30 in expiration_days, f"Expected custom 30-day expiration, got {expiration_days!r}"
        assert 90 not in expiration_days, (
            "Default 90-day expiration should not be present when retention_days=30"
        )

    def test_both_buckets_block_public_access(self):
        """Both the model weights bucket and the access-logs bucket block all public access."""
        template = _synth(cdk.App())

        expected_block = {
            "BlockPublicAcls": True,
            "BlockPublicPolicy": True,
            "IgnorePublicAcls": True,
            "RestrictPublicBuckets": True,
        }

        for bucket in (
            _find_model_weights_bucket(template),
            _find_access_logs_bucket(template),
        ):
            pab = bucket["Properties"].get("PublicAccessBlockConfiguration")
            assert pab == expected_block, f"Bucket does not fully block public access; got {pab!r}"
