"""Each workload Region's persistent bucket: staging that survives ``gco upgrade``.

``gco upgrade`` and ``gco stacks destroy-all --keep-control-plane`` delete every
regional stack, and with it the regional-shared bucket, EFS, FSx and the
in-cluster volumes. This stack, ``<project>-<region>-persistent``, sits in the
control-plane tier next to the global stacks, so those teardowns leave it in
place. It holds one bucket per Region, in the Region's own S3, where jobs and
operators can deposit outputs before the teardown and read them back once the
regional stack has been rebuilt. A full ``gco stacks destroy-all`` deletes it,
objects included, like every other GCO bucket.

``cdk.json::regional_persistent_bucket`` turns it on (``enabled``) and can expire
objects after N days (``expiration_days``); see
:meth:`gco.config.config_loader.ConfigLoader.get_regional_persistent_bucket_config`.
The regional stack of the same Region reads the bucket's identity back from the
SSM parameters published here (``regional_persistent_ssm_parameter_prefix``),
grants its job-pod role read/write on the bucket and use of its key, and fills
the ``gco-regional-persistent-bucket`` ConfigMap in ``gco-jobs``.
"""

from __future__ import annotations

from typing import Any

from aws_cdk import Duration, RemovalPolicy, Stack
from aws_cdk import aws_iam as iam
from aws_cdk import aws_kms as kms
from aws_cdk import aws_s3 as s3
from aws_cdk import aws_ssm as ssm
from constructs import Construct

from gco.config.config_loader import ConfigLoader
from gco.stacks.constants import regional_persistent_ssm_parameter_prefix

#: Days S3 keeps an object version after it was overwritten or deleted. The
#: bucket is versioned so an accidental overwrite of staged data can be undone;
#: this bounds what the old versions cost.
NONCURRENT_VERSION_RETENTION_DAYS = 30

#: Days after which S3 aborts a multipart upload that never completed, such as
#: one a ``gco storage sync`` interrupted. Its parts are billed until then.
INCOMPLETE_UPLOAD_RETENTION_DAYS = 7


def regional_persistent_stack_name(project_name: str, region: str) -> str:
    """The stack name for ``region``'s persistent bucket: ``<project>-<region>-persistent``.

    The Region comes before the suffix so that no other project's stack can
    share the name: a regional stack ``<other>-<region>`` ends in a digit, and a
    regional API bridge ``<other>-regional-api-<region>`` does too.
    """
    return f"{project_name}-{region}-persistent"


def persistent_bucket_lifecycle_rules(expiration_days: int | None) -> list[s3.LifecycleRule]:
    """The bucket's lifecycle rules for ``regional_persistent_bucket.expiration_days``.

    Always: abort incomplete multipart uploads after
    :data:`INCOMPLETE_UPLOAD_RETENTION_DAYS`, and expire noncurrent versions after
    :data:`NONCURRENT_VERSION_RETENTION_DAYS` together with the delete markers
    left once a key has no versions. With ``expiration_days`` set, current
    objects also expire that many days after they were written; S3 then keeps
    the expired version as a noncurrent one for the noncurrent retention.
    """
    rules = [
        s3.LifecycleRule(
            id="AbortIncompleteUploads",
            enabled=True,
            abort_incomplete_multipart_upload_after=Duration.days(INCOMPLETE_UPLOAD_RETENTION_DAYS),
        ),
        s3.LifecycleRule(
            id="ExpireNoncurrentVersions",
            enabled=True,
            noncurrent_version_expiration=Duration.days(NONCURRENT_VERSION_RETENTION_DAYS),
            expired_object_delete_marker=True,
        ),
    ]
    if expiration_days is not None:
        rules.append(
            s3.LifecycleRule(
                id="ExpireObjects",
                enabled=True,
                expiration=Duration.days(expiration_days),
            )
        )
    return rules


class GCORegionalPersistentStack(Stack):
    """The persistent bucket, its KMS key and its access-logs bucket for one Region."""

    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        config: ConfigLoader,
        region: str,
        **kwargs: Any,
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)
        self.config = config
        self.deployment_region = region
        project_name = config.get_project_name()
        settings = config.get_regional_persistent_bucket_config()

        # Same posture as the regional-shared key: annual rotation, the 7-day
        # minimum pending window, deleted with the stack. The default key
        # policy delegates to IAM, and S3 uses the key with the requesting
        # principal's own permissions.
        self.kms_key = kms.Key(
            self,
            "PersistentBucketKey",
            description=(
                f"Customer-managed KMS key for the persistent bucket of {project_name} "
                f"in {region}, which survives gco upgrade."
            ),
            enable_key_rotation=True,
            pending_window=Duration.days(7),
            removal_policy=RemovalPolicy.DESTROY,
        )

        s3_access_logs_ctx = self.node.try_get_context("s3_access_logs") or {}
        access_logs_retention_days = int(s3_access_logs_ctx.get("retention_days", 90))
        # SSE-S3: S3 server access logging requires an SSE-S3 destination.
        self.access_logs_bucket = s3.Bucket(
            self,
            "PersistentAccessLogsBucket",
            encryption=s3.BucketEncryption.S3_MANAGED,
            block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
            enforce_ssl=True,
            versioned=True,
            removal_policy=RemovalPolicy.DESTROY,
            auto_delete_objects=True,
            lifecycle_rules=[
                s3.LifecycleRule(
                    id="ExpireAccessLogs",
                    enabled=True,
                    expiration=Duration.days(access_logs_retention_days),
                )
            ],
        )

        # No ``bucket_name``: CloudFormation generates it, so a destroy and
        # redeploy can never collide in S3's global namespace (ADR-0005).
        # Consumers read it from the SSM parameters below.
        self.bucket = s3.Bucket(
            self,
            "PersistentBucket",
            encryption=s3.BucketEncryption.KMS,
            encryption_key=self.kms_key,
            bucket_key_enabled=True,
            block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
            enforce_ssl=True,
            versioned=True,
            removal_policy=RemovalPolicy.DESTROY,
            auto_delete_objects=True,
            server_access_logs_bucket=self.access_logs_bucket,
            server_access_logs_prefix="regional-persistent/",
            lifecycle_rules=persistent_bucket_lifecycle_rules(settings["expiration_days"]),
        )
        # Explicit Deny for insecure transport under a known SID, as on the
        # other GCO buckets (enforce_ssl adds an equivalent statement).
        self.bucket.add_to_resource_policy(
            iam.PolicyStatement(
                sid="DenyInsecureTransport",
                effect=iam.Effect.DENY,
                principals=[iam.AnyPrincipal()],
                actions=["s3:*"],
                resources=[self.bucket.bucket_arn, f"{self.bucket.bucket_arn}/*"],
                conditions={"Bool": {"aws:SecureTransport": "false"}},
            )
        )

        prefix = regional_persistent_ssm_parameter_prefix(project_name)
        for suffix, value, what in (
            ("name", self.bucket.bucket_name, "Name"),
            ("arn", self.bucket.bucket_arn, "ARN"),
            ("region", region, "Home Region"),
            ("kms-key-arn", self.kms_key.key_arn, "ARN of the KMS key"),
        ):
            ssm.StringParameter(
                self,
                f"PersistentBucket{''.join(part.capitalize() for part in suffix.split('-'))}Param",
                parameter_name=f"{prefix}/{suffix}",
                string_value=value,
                description=f"{what} of this Region's persistent bucket, which survives gco upgrade.",
            )

        self._acknowledge_nag_rules()

    def _acknowledge_nag_rules(self) -> None:
        """Record why the bucket pair does not replicate and the logs bucket does not log."""
        from gco.stacks.nag_suppressions import (
            acknowledge_access_log_destination_encryption,
            acknowledge_nag_findings,
            apply_all_suppressions,
        )

        replication_reason = (
            "The persistent bucket stages a Region's outputs across a gco upgrade, in that "
            "Region; nothing requires a copy in another Region, and its access logs follow it."
        )
        replication_rules = [
            {"id": f"{pack}-S3BucketReplicationEnabled", "reason": replication_reason}
            for pack in ("HIPAA.Security", "NIST.800.53.R5", "PCI.DSS.321")
        ]
        acknowledge_nag_findings(self.bucket, replication_rules)
        logs_reason = "This is the server access logs destination of the persistent bucket."
        acknowledge_nag_findings(
            self.access_logs_bucket,
            [
                {"id": "AwsSolutions-S1", "reason": logs_reason},
                *(
                    {"id": f"{pack}-S3BucketLoggingEnabled", "reason": logs_reason}
                    for pack in ("HIPAA.Security", "NIST.800.53.R5", "PCI.DSS.321")
                ),
                *replication_rules,
            ],
        )
        acknowledge_access_log_destination_encryption(self.access_logs_bucket)
        apply_all_suppressions(
            self,
            stack_type="regional_persistent",
            global_region=self.config.get_global_region(),
            project_name=self.config.get_project_name(),
        )
