"""
Monitoring stack for GCO (Global Capacity Orchestrator on AWS) - Cross-region monitoring and observability.

This stack creates centralized monitoring resources for all GCO deployments:
- CloudWatch Dashboard with comprehensive widgets for all regions
- SNS topic for alerting
- CloudWatch Alarms for critical metrics
- Log groups for application logs
- Anomaly detection for traffic patterns
- Composite alarms for better signal-to-noise

Dashboard Sections:
- Global Accelerator: Flow counts, processed bytes
- API Gateway: Request counts, latency, error rates
- Lambda Functions: Invocations, errors, duration, throttles
- SQS Queues: Message counts, age, dead letter queue depth
- DynamoDB Tables: Capacity, latency, throttles, errors
- EKS Clusters: CPU/memory utilization per region
- FSx for Lustre (when enabled): Throughput, IOPS, free storage
- Valkey Serverless (when enabled): ECPU, hit rate, latency, bytes used
- Aurora pgvector (when enabled): ACU utilization, connections, latency, CPU
- ALBs: Request counts, response times, healthy hosts
- Applications: Custom metrics from health monitor and manifest processor

Cross-Region Metrics:
    CloudWatch metrics are region-specific. This stack handles cross-region
    monitoring by specifying the `region` parameter on metrics:
    - Global Accelerator metrics: Always in us-west-2
    - DynamoDB metrics: In the global region (where tables are deployed)
    - Regional metrics: In each cluster's region

Alarms:
- High CPU/memory utilization on EKS clusters
- Unhealthy hosts in ALB target groups
- High response times
- Manifest processing failures
- Lambda errors and throttles
- SQS message age (stuck jobs)
- DynamoDB throttling and system errors
- API Gateway 5XX errors
- Secret rotation failures
"""

from typing import TYPE_CHECKING, Any

from aws_cdk import (
    CfnOutput,
    Duration,
    RemovalPolicy,
    Stack,
)
from aws_cdk import aws_athena as athena
from aws_cdk import aws_cloudwatch as cloudwatch
from aws_cdk import aws_cloudwatch_actions as cw_actions
from aws_cdk import aws_glue as glue
from aws_cdk import aws_iam as iam
from aws_cdk import aws_kms as kms
from aws_cdk import aws_logs as logs
from aws_cdk import aws_s3 as s3
from aws_cdk import aws_sns as sns
from constructs import Construct

from gco.config.config_loader import ConfigLoader
from gco.stacks.constants import (
    COST_ATHENA_RESULTS_PREFIX,
    COST_GLUE_ALLOCATION_TABLE,
    COST_REPORT_SCHEDULED_PREFIX,
    cost_athena_workgroup_name,
    cost_glue_database_name,
    cost_report_bucket_name,
)

# <pyflowchart-code-diagram> BEGIN - auto-inserted, do not edit
# Generated at (UTC): 2026-07-18T01:03:40Z
# Flowchart(s) generated from this file:
#   * ``GCOMonitoringStack.__init__`` -> ``diagrams/code_diagrams/gco/stacks/monitoring_stack.GCOMonitoringStack___init__.html``
#     (PNG: ``diagrams/code_diagrams/gco/stacks/monitoring_stack.GCOMonitoringStack___init__.png``)
# Regenerate with ``python diagrams/code_diagrams/generate.py``.
# <pyflowchart-code-diagram> END


if TYPE_CHECKING:
    from gco.stacks.api_gateway_global_stack import GCOApiGatewayGlobalStack
    from gco.stacks.global_stack import GCOGlobalStack
    from gco.stacks.regional_stack import GCORegionalStack


class GCOMonitoringStack(Stack):
    """
    Cross-region monitoring and observability stack.

    Creates a centralized CloudWatch dashboard and alarms that aggregate
    metrics from all regional deployments.

    Attributes:
        alert_topic: SNS topic for alarm notifications
        dashboard: CloudWatch dashboard with all monitoring widgets
    """

    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        config: ConfigLoader,
        global_stack: GCOGlobalStack,
        regional_stacks: list[GCORegionalStack],
        api_gateway_stack: GCOApiGatewayGlobalStack | None = None,
        **kwargs: Any,
    ) -> None:
        # Enable CDK's native cross-region references. The monitoring stack
        # lives in the monitoring region (by default us-east-2) and needs
        # resource identifiers from the regional stacks for dashboard
        # dimensions — specifically the auto-generated FSx file system IDs,
        # whose values aren't known until deploy time.
        #
        # CDK implements this by provisioning a small Lambda-backed custom
        # resource in each source stack that writes the referenced value to
        # an SSM parameter in the target region, plus a reader custom
        # resource in the target stack. Cost is negligible (the Lambdas run
        # once per deploy) and the pattern is the documented canonical
        # answer for ``CrossRegionReferencesNotEnabled`` errors.
        kwargs.setdefault("cross_region_references", True)
        super().__init__(scope, construct_id, **kwargs)

        self.config = config
        self.global_stack = global_stack
        self.regional_stacks = regional_stacks
        self.api_gateway_stack = api_gateway_stack
        self.project_name = config.get_project_name()
        self.regions = config.get_regions()

        # Create SNS topic for alerts
        self.alert_topic = self._create_alert_topic()

        # Cost monitoring pipeline (on by default): the cost report bucket the
        # regional cost-monitor services write Parquet allocation reports to,
        # plus the Glue database/table and Athena workgroup that make the
        # cross-region data queryable from the CLI.
        if self.config.get_cost_monitoring_enabled():
            self._create_cost_report_storage()
            self._create_cost_analytics()

        # Create CloudWatch dashboard
        self.dashboard = self._create_dashboard()

        # Create alarms
        self._create_alarms()

        # Create composite alarms
        self._create_composite_alarms()

        # Create custom metrics
        self._create_custom_metrics()

        # Export monitoring resources
        self._create_outputs()

        # Apply cdk-nag suppressions
        self._apply_nag_suppressions()

    def _apply_nag_suppressions(self) -> None:
        """Apply cdk-nag suppressions for this stack."""
        from gco.stacks.nag_suppressions import apply_all_suppressions

        apply_all_suppressions(
            self,
            stack_type="monitoring",
            regions=self.config.get_regions(),
            global_region=self.config.get_global_region(),
            project_name=self.project_name,
        )

    def _create_alert_topic(self) -> sns.Topic:
        """Create SNS topic for monitoring alerts"""
        topic = sns.Topic(
            self,
            "GCOAlertTopic",
            display_name="GCO (Global Capacity Orchestrator on AWS) Monitoring Alerts",
            enforce_ssl=True,
        )
        return topic

    def _create_cost_report_storage(self) -> None:
        """Create the central cost report bucket for the cost monitoring pipeline.

        Every regional cost-monitor service writes Hive-partitioned Parquet
        allocation reports here:

        - ``reports/region=<region>/date=<YYYY-MM-DD>/...`` — scheduled
          reports; the Glue table's partition projection reads this layout.
        - ``adhoc/region=<region>/date=<YYYY-MM-DD>/...`` — user-requested
          reports, kept out of the scheduled table so overlapping windows can
          never double-count in Athena aggregations.
        - ``athena-results/`` — Athena query results for the cost workgroup.

        Three constructs mirror the regional-shared bucket pattern:

        1. ``cost_report_kms_key`` — customer-managed KMS key with annual
           rotation and a 7-day pending window on destroy.
        2. ``cost_report_access_logs_bucket`` — the dedicated S3 access-logs
           destination for the primary bucket.
        3. ``cost_report_bucket`` — the primary bucket named
           ``<project>-cost-reports-<account>-<monitoring-region>``. The name
           is fully deterministic (``cost_report_bucket_name``) because the
           regional stacks — which deploy *before* this stack — grant their
           cost-monitor roles write access by literal ARN.

        Lifecycle policy comes from ``cdk.json`` (``cost_monitoring.reports``):
        report objects transition to STANDARD_IA after
        ``transition_to_infrequent_access_days`` and expire after
        ``retention_days``; Athena results expire after
        ``athena.query_results_retention_days``.
        """
        cost_config = self.config.get_cost_monitoring_config()
        reports_config = cost_config["reports"]
        athena_config = cost_config["athena"]

        # KMS key for the cost report bucket. Annual rotation, 7-day pending
        # window, destroy-on-teardown — matching the shared-bucket posture.
        self.cost_report_kms_key = kms.Key(
            self,
            "CostReportKmsKey",
            description=(
                "Customer-managed KMS key for the GCO cost report bucket in the monitoring stack."
            ),
            enable_key_rotation=True,
            pending_window=Duration.days(7),
            removal_policy=RemovalPolicy.DESTROY,
        )

        kms_actions = [
            "kms:Encrypt",
            "kms:Decrypt",
            "kms:ReEncrypt*",
            "kms:GenerateDataKey*",
            "kms:DescribeKey",
        ]
        self.cost_report_kms_key.add_to_resource_policy(
            iam.PolicyStatement(
                sid="AllowS3ServiceEncryptDecrypt",
                effect=iam.Effect.ALLOW,
                principals=[iam.ServicePrincipal("s3.amazonaws.com")],
                actions=kms_actions,
                resources=["*"],
            )
        )

        # Retention for the access-logs bucket honors the same `s3_access_logs`
        # context field used by the central buckets (default 90 days).
        s3_access_logs_ctx = self.node.try_get_context("s3_access_logs") or {}
        access_logs_retention_days = int(s3_access_logs_ctx.get("retention_days", 90))

        self.cost_report_access_logs_bucket = s3.Bucket(
            self,
            "CostReportAccessLogsBucket",
            encryption=s3.BucketEncryption.KMS,
            encryption_key=self.cost_report_kms_key,
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

        self.cost_report_bucket = s3.Bucket(
            self,
            "CostReportBucket",
            bucket_name=cost_report_bucket_name(self.project_name, self.account, self.region),
            encryption=s3.BucketEncryption.KMS,
            encryption_key=self.cost_report_kms_key,
            bucket_key_enabled=True,
            block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
            enforce_ssl=True,
            versioned=True,
            removal_policy=RemovalPolicy.DESTROY,
            auto_delete_objects=True,
            server_access_logs_bucket=self.cost_report_access_logs_bucket,
            server_access_logs_prefix="cost-reports/",
            lifecycle_rules=[
                # Scheduled + ad-hoc reports share one policy: IA after the
                # configured transition, expiry after the retention window.
                s3.LifecycleRule(
                    id="CostReportRetention",
                    enabled=True,
                    prefix=f"{COST_REPORT_SCHEDULED_PREFIX}/",
                    transitions=[
                        s3.Transition(
                            storage_class=s3.StorageClass.INFREQUENT_ACCESS,
                            transition_after=Duration.days(
                                int(reports_config["transition_to_infrequent_access_days"])
                            ),
                        )
                    ],
                    expiration=Duration.days(int(reports_config["retention_days"])),
                ),
                s3.LifecycleRule(
                    id="AdhocReportRetention",
                    enabled=True,
                    prefix="adhoc/",
                    transitions=[
                        s3.Transition(
                            storage_class=s3.StorageClass.INFREQUENT_ACCESS,
                            transition_after=Duration.days(
                                int(reports_config["transition_to_infrequent_access_days"])
                            ),
                        )
                    ],
                    expiration=Duration.days(int(reports_config["retention_days"])),
                ),
                s3.LifecycleRule(
                    id="ExpireAthenaResults",
                    enabled=True,
                    prefix=f"{COST_ATHENA_RESULTS_PREFIX}/",
                    expiration=Duration.days(int(athena_config["query_results_retention_days"])),
                ),
            ],
        )

        # Explicit Deny for insecure transport with a verifiable SID,
        # duplicating enforce_ssl=True per the central-bucket pattern.
        self.cost_report_bucket.add_to_resource_policy(
            iam.PolicyStatement(
                sid="DenyInsecureTransport",
                effect=iam.Effect.DENY,
                principals=[iam.AnyPrincipal()],
                actions=["s3:*"],
                resources=[
                    self.cost_report_bucket.bucket_arn,
                    f"{self.cost_report_bucket.bucket_arn}/*",
                ],
                conditions={"Bool": {"aws:SecureTransport": "false"}},
            )
        )

        from gco.stacks.nag_suppressions import acknowledge_nag_findings

        cost_replication_reason = (
            "Cost reports are derived analytics data regenerated continuously "
            "by the per-region cost-monitor services; there is no durability "
            "requirement that warrants cross-region replication. Access logs "
            "do not require replication for the same reason."
        )
        acknowledge_nag_findings(
            self.cost_report_bucket,
            [
                {
                    "id": "HIPAA.Security-S3BucketReplicationEnabled",
                    "reason": cost_replication_reason,
                },
                {
                    "id": "NIST.800.53.R5-S3BucketReplicationEnabled",
                    "reason": cost_replication_reason,
                },
                {
                    "id": "PCI.DSS.321-S3BucketReplicationEnabled",
                    "reason": cost_replication_reason,
                },
            ],
        )

        access_logs_is_self_target_reason = (
            "This is the server access logs destination bucket for the cost report bucket."
        )
        acknowledge_nag_findings(
            self.cost_report_access_logs_bucket,
            [
                {
                    "id": "AwsSolutions-S1",
                    "reason": access_logs_is_self_target_reason,
                },
                {
                    "id": "HIPAA.Security-S3BucketLoggingEnabled",
                    "reason": access_logs_is_self_target_reason,
                },
                {
                    "id": "NIST.800.53.R5-S3BucketLoggingEnabled",
                    "reason": access_logs_is_self_target_reason,
                },
                {
                    "id": "PCI.DSS.321-S3BucketLoggingEnabled",
                    "reason": access_logs_is_self_target_reason,
                },
                {
                    "id": "HIPAA.Security-S3BucketReplicationEnabled",
                    "reason": cost_replication_reason,
                },
                {
                    "id": "NIST.800.53.R5-S3BucketReplicationEnabled",
                    "reason": cost_replication_reason,
                },
                {
                    "id": "PCI.DSS.321-S3BucketReplicationEnabled",
                    "reason": cost_replication_reason,
                },
            ],
        )

        CfnOutput(
            self,
            "CostReportBucketName",
            value=self.cost_report_bucket.bucket_name,
            description="Central S3 bucket receiving per-region Parquet cost reports",
        )

    def _create_cost_analytics(self) -> None:
        """Create the Glue database/table and Athena workgroup for cost queries.

        The Glue table reads the scheduled report layout
        (``reports/region=<region>/date=<YYYY-MM-DD>/*.parquet``) using
        **partition projection**, so there is no crawler, no scheduled
        ``MSCK REPAIR``, and no partition-management Lambda — new partitions
        are queryable the moment an object lands. The ``region`` partition is
        projected from the configured deployment regions; ``date`` is a native
        date projection from 2026-01-01 to NOW.

        The Athena workgroup pins query results to ``athena-results/`` in the
        cost report bucket (KMS-encrypted, lifecycle-expired) and enforces its
        configuration so callers cannot redirect results elsewhere.

        The table schema is the write-side contract of
        ``gco.services.cost_monitor`` — the columns below must stay in
        lockstep with the Parquet fields the service emits.
        """
        database_name = cost_glue_database_name(self.project_name)

        self.cost_glue_database = glue.CfnDatabase(
            self,
            "CostGlueDatabase",
            catalog_id=self.account,
            database_input=glue.CfnDatabase.DatabaseInputProperty(
                name=database_name,
                description=(
                    "GCO cost analytics: per-region OpenCost allocation "
                    "reports written by the cost-monitor services."
                ),
            ),
        )

        # Columns mirror gco/services/cost_monitor.py::ALLOCATION_REPORT_FIELDS.
        columns = [
            glue.CfnTable.ColumnProperty(name="window_start", type="timestamp"),
            glue.CfnTable.ColumnProperty(name="window_end", type="timestamp"),
            glue.CfnTable.ColumnProperty(name="cluster", type="string"),
            glue.CfnTable.ColumnProperty(name="namespace", type="string"),
            glue.CfnTable.ColumnProperty(name="cpu_core_hours", type="double"),
            glue.CfnTable.ColumnProperty(name="cpu_cost", type="double"),
            glue.CfnTable.ColumnProperty(name="ram_gib_hours", type="double"),
            glue.CfnTable.ColumnProperty(name="ram_cost", type="double"),
            glue.CfnTable.ColumnProperty(name="gpu_hours", type="double"),
            glue.CfnTable.ColumnProperty(name="gpu_cost", type="double"),
            glue.CfnTable.ColumnProperty(name="pv_cost", type="double"),
            glue.CfnTable.ColumnProperty(name="network_cost", type="double"),
            glue.CfnTable.ColumnProperty(name="load_balancer_cost", type="double"),
            glue.CfnTable.ColumnProperty(name="shared_cost", type="double"),
            glue.CfnTable.ColumnProperty(name="external_cost", type="double"),
            glue.CfnTable.ColumnProperty(name="total_cost", type="double"),
            glue.CfnTable.ColumnProperty(name="total_efficiency", type="double"),
        ]

        scheduled_location = (
            f"s3://{self.cost_report_bucket.bucket_name}/{COST_REPORT_SCHEDULED_PREFIX}/"
        )
        self.cost_glue_table = glue.CfnTable(
            self,
            "CostAllocationTable",
            catalog_id=self.account,
            database_name=database_name,
            table_input=glue.CfnTable.TableInputProperty(
                name=COST_GLUE_ALLOCATION_TABLE,
                description="Scheduled OpenCost allocation reports (Parquet)",
                table_type="EXTERNAL_TABLE",
                parameters={
                    "classification": "parquet",
                    "EXTERNAL": "TRUE",
                    # Partition projection: no crawler, no MSCK. The region
                    # projection is pinned to the deployment's configured
                    # regions; extend deployment_regions.regional and redeploy
                    # to pick up new regions.
                    "projection.enabled": "true",
                    "projection.region.type": "enum",
                    "projection.region.values": ",".join(self.regions),
                    "projection.date.type": "date",
                    "projection.date.range": "2026-01-01,NOW",
                    "projection.date.format": "yyyy-MM-dd",
                    "projection.date.interval": "1",
                    "projection.date.interval.unit": "DAYS",
                    "storage.location.template": (
                        f"{scheduled_location}region=${{region}}/date=${{date}}"
                    ),
                },
                partition_keys=[
                    glue.CfnTable.ColumnProperty(name="region", type="string"),
                    glue.CfnTable.ColumnProperty(name="date", type="string"),
                ],
                storage_descriptor=glue.CfnTable.StorageDescriptorProperty(
                    location=scheduled_location,
                    input_format=("org.apache.hadoop.hive.ql.io.parquet.MapredParquetInputFormat"),
                    output_format=(
                        "org.apache.hadoop.hive.ql.io.parquet.MapredParquetOutputFormat"
                    ),
                    serde_info=glue.CfnTable.SerdeInfoProperty(
                        serialization_library=(
                            "org.apache.hadoop.hive.ql.io.parquet.serde.ParquetHiveSerDe"
                        ),
                    ),
                    columns=columns,
                ),
            ),
        )
        self.cost_glue_table.add_resource_dependency(self.cost_glue_database)

        self.cost_athena_workgroup = athena.CfnWorkGroup(
            self,
            "CostAthenaWorkGroup",
            name=cost_athena_workgroup_name(self.project_name),
            description="GCO cost analytics queries over the cost report bucket",
            recursive_delete_option=True,
            work_group_configuration=athena.CfnWorkGroup.WorkGroupConfigurationProperty(
                enforce_work_group_configuration=True,
                publish_cloud_watch_metrics_enabled=True,
                result_configuration=athena.CfnWorkGroup.ResultConfigurationProperty(
                    output_location=(
                        f"s3://{self.cost_report_bucket.bucket_name}/{COST_ATHENA_RESULTS_PREFIX}/"
                    ),
                    encryption_configuration=(
                        athena.CfnWorkGroup.EncryptionConfigurationProperty(
                            encryption_option="SSE_KMS",
                            kms_key=self.cost_report_kms_key.key_arn,
                        )
                    ),
                ),
            ),
        )

        CfnOutput(
            self,
            "CostAthenaWorkGroupName",
            value=cost_athena_workgroup_name(self.project_name),
            description="Athena workgroup for GCO cost analytics queries",
        )
        CfnOutput(
            self,
            "CostGlueDatabaseName",
            value=cost_glue_database_name(self.project_name),
            description="Glue database containing the cost allocation table",
        )

    def _create_dashboard(self) -> cloudwatch.Dashboard:
        """Create comprehensive CloudWatch dashboard for monitoring"""
        dashboard = cloudwatch.Dashboard(
            self,
            "GCODashboard",
            period_override=cloudwatch.PeriodOverride.AUTO,
        )

        # Add widgets in logical order
        dashboard.add_widgets(*self._create_global_accelerator_widgets())
        dashboard.add_widgets(*self._create_api_gateway_widgets())
        dashboard.add_widgets(*self._create_lambda_widgets())
        dashboard.add_widgets(*self._create_sqs_widgets())
        dashboard.add_widgets(*self._create_dynamodb_widgets())
        dashboard.add_widgets(*self._create_eks_widgets())
        dashboard.add_widgets(*self._create_gpu_widgets())
        dashboard.add_widgets(*self._create_fsx_widgets())
        dashboard.add_widgets(*self._create_valkey_widgets())
        dashboard.add_widgets(*self._create_aurora_pgvector_widgets())
        dashboard.add_widgets(*self._create_alb_widgets())
        dashboard.add_widgets(*self._create_application_widgets())

        return dashboard

    def _create_global_accelerator_widgets(self) -> list[cloudwatch.IWidget]:
        """Create Global Accelerator monitoring widgets.

        Note: Global Accelerator metrics are only available in us-west-2,
        regardless of where the accelerator endpoints are located.
        CloudWatch uses the Accelerator ID (UUID), not the name.
        """
        widgets: list[cloudwatch.IWidget] = []
        if self.global_stack.accelerator_id is None:
            return widgets

        # Get the accelerator ID from the global stack (CloudWatch uses ID, not name)
        accelerator_id = self.global_stack.accelerator_id

        # Global Accelerator metrics are always in us-west-2
        ga_metrics_region = "us-west-2"

        # Section header
        widgets.append(
            cloudwatch.TextWidget(
                markdown="# Global Accelerator\nTraffic distribution and connectivity metrics",
                width=24,
                height=1,
            )
        )

        # Flow count with anomaly detection
        flow_count_widget = cloudwatch.GraphWidget(
            title="Global Accelerator - New Flows",
            left=[
                cloudwatch.Metric(
                    namespace="AWS/GlobalAccelerator",
                    metric_name="NewFlowCount",
                    dimensions_map={"Accelerator": accelerator_id},
                    statistic="Sum",
                    period=Duration.minutes(5),
                    region=ga_metrics_region,
                )
            ],
            width=12,
            height=6,
            region=ga_metrics_region,
        )
        widgets.append(flow_count_widget)

        # Processed bytes
        bytes_widget = cloudwatch.GraphWidget(
            title="Global Accelerator - Processed Bytes",
            left=[
                cloudwatch.Metric(
                    namespace="AWS/GlobalAccelerator",
                    metric_name="ProcessedBytesIn",
                    dimensions_map={"Accelerator": accelerator_id},
                    statistic="Sum",
                    period=Duration.minutes(5),
                    region=ga_metrics_region,
                ),
                cloudwatch.Metric(
                    namespace="AWS/GlobalAccelerator",
                    metric_name="ProcessedBytesOut",
                    dimensions_map={"Accelerator": accelerator_id},
                    statistic="Sum",
                    period=Duration.minutes(5),
                    region=ga_metrics_region,
                ),
            ],
            width=12,
            height=6,
            region=ga_metrics_region,
        )
        widgets.append(bytes_widget)

        return widgets

    def _create_api_gateway_widgets(self) -> list[cloudwatch.IWidget]:
        """Create API Gateway monitoring widgets"""
        widgets: list[cloudwatch.IWidget] = []

        # Get the actual API name from the api_gateway_stack
        api_name = (
            self.api_gateway_stack.api.rest_api_name
            if self.api_gateway_stack
            else f"{self.project_name}-global-api"
        )

        # API Gateway metrics are in the region where the API is deployed
        api_gw_region = self.config.get_api_gateway_region()

        # Section header
        widgets.append(
            cloudwatch.TextWidget(
                markdown="# API Gateway\nRequest metrics, latency, and error rates",
                width=24,
                height=1,
            )
        )

        # Request count and latency
        request_widget = cloudwatch.GraphWidget(
            title="API Gateway - Requests & Latency",
            left=[
                cloudwatch.Metric(
                    namespace="AWS/ApiGateway",
                    metric_name="Count",
                    dimensions_map={"ApiName": api_name},
                    statistic="Sum",
                    period=Duration.minutes(5),
                    region=api_gw_region,
                )
            ],
            right=[
                cloudwatch.Metric(
                    namespace="AWS/ApiGateway",
                    metric_name="Latency",
                    dimensions_map={"ApiName": api_name},
                    statistic="Average",
                    period=Duration.minutes(5),
                    region=api_gw_region,
                ),
                cloudwatch.Metric(
                    namespace="AWS/ApiGateway",
                    metric_name="Latency",
                    dimensions_map={"ApiName": api_name},
                    statistic="p99",
                    period=Duration.minutes(5),
                    region=api_gw_region,
                ),
            ],
            width=12,
            height=6,
            region=api_gw_region,
        )
        widgets.append(request_widget)

        # Error rates (4XX and 5XX)
        error_widget = cloudwatch.GraphWidget(
            title="API Gateway - Error Rates",
            left=[
                cloudwatch.Metric(
                    namespace="AWS/ApiGateway",
                    metric_name="4XXError",
                    dimensions_map={"ApiName": api_name},
                    statistic="Sum",
                    period=Duration.minutes(5),
                    color="#ff7f0e",
                    region=api_gw_region,
                ),
                cloudwatch.Metric(
                    namespace="AWS/ApiGateway",
                    metric_name="5XXError",
                    dimensions_map={"ApiName": api_name},
                    statistic="Sum",
                    period=Duration.minutes(5),
                    color="#d62728",
                    region=api_gw_region,
                ),
            ],
            width=12,
            height=6,
            region=api_gw_region,
        )
        widgets.append(error_widget)

        return widgets

    def _create_lambda_widgets(self) -> list[cloudwatch.IWidget]:
        """Create Lambda function monitoring widgets"""
        widgets: list[cloudwatch.IWidget] = []

        # Section header
        widgets.append(
            cloudwatch.TextWidget(
                markdown="# Lambda Functions\nProxy, rotation, and regional Lambda metrics",
                width=24,
                height=1,
            )
        )

        # Get API Gateway region for global Lambda functions
        api_gw_region = self.config.get_api_gateway_region()

        # Build Lambda function list: (function_name, label, region)
        lambda_functions: list[tuple[str, str, str]] = []

        # Add API Gateway Lambda functions if available
        if self.api_gateway_stack:
            if self.api_gateway_stack.proxy_lambda is not None:
                lambda_functions.append(
                    (
                        self.api_gateway_stack.proxy_lambda.function_name,
                        "API Gateway Proxy",
                        api_gw_region,
                    )
                )
            lambda_functions.append(
                (
                    self.api_gateway_stack.rotation_lambda.function_name,
                    "Secret Rotation",
                    api_gw_region,
                )
            )

        # Add regional Lambda functions from each regional stack
        for regional_stack in self.regional_stacks:
            region = regional_stack.deployment_region
            lambda_functions.extend(
                [
                    (
                        regional_stack.kubectl_lambda_function_name,
                        f"Kubectl Applier ({region})",
                        region,
                    ),
                    (
                        regional_stack.helm_installer_lambda_function_name,
                        f"Helm Installer ({region})",
                        region,
                    ),
                ]
            )

        # Invocations widget
        invocations_widget = cloudwatch.GraphWidget(
            title="Lambda - Invocations",
            left=[
                cloudwatch.Metric(
                    namespace="AWS/Lambda",
                    metric_name="Invocations",
                    dimensions_map={"FunctionName": func_name},
                    statistic="Sum",
                    period=Duration.minutes(5),
                    label=label,
                    region=region,
                )
                for func_name, label, region in lambda_functions[:5]
            ],
            width=12,
            height=6,
        )
        widgets.append(invocations_widget)

        errors_widget = cloudwatch.GraphWidget(
            title="Lambda - Errors",
            left=[
                cloudwatch.Metric(
                    namespace="AWS/Lambda",
                    metric_name="Errors",
                    dimensions_map={"FunctionName": func_name},
                    statistic="Sum",
                    period=Duration.minutes(5),
                    label=label,
                    color="#d62728",
                    region=region,
                )
                for func_name, label, region in lambda_functions[:5]
            ],
            width=12,
            height=6,
        )
        widgets.append(errors_widget)

        # Duration widget
        duration_widget = cloudwatch.GraphWidget(
            title="Lambda - Duration (ms)",
            left=[
                cloudwatch.Metric(
                    namespace="AWS/Lambda",
                    metric_name="Duration",
                    dimensions_map={"FunctionName": func_name},
                    statistic="Average",
                    period=Duration.minutes(5),
                    label=label,
                    region=region,
                )
                for func_name, label, region in lambda_functions[:5]
            ],
            width=12,
            height=6,
        )
        widgets.append(duration_widget)

        # Throttles widget
        throttles_widget = cloudwatch.GraphWidget(
            title="Lambda - Throttles & Concurrent Executions",
            left=[
                cloudwatch.Metric(
                    namespace="AWS/Lambda",
                    metric_name="Throttles",
                    dimensions_map={"FunctionName": func_name},
                    statistic="Sum",
                    period=Duration.minutes(5),
                    label=f"{label} Throttles",
                    region=region,
                )
                for func_name, label, region in lambda_functions[:3]
            ],
            right=[
                cloudwatch.Metric(
                    namespace="AWS/Lambda",
                    metric_name="ConcurrentExecutions",
                    dimensions_map={"FunctionName": func_name},
                    statistic="Maximum",
                    period=Duration.minutes(5),
                    label=f"{label} Concurrent",
                    region=region,
                )
                for func_name, label, region in lambda_functions[:3]
            ],
            width=12,
            height=6,
        )
        widgets.append(throttles_widget)

        return widgets

    def _create_sqs_widgets(self) -> list[cloudwatch.IWidget]:
        """Create SQS queue monitoring widgets"""
        widgets: list[cloudwatch.IWidget] = []

        # Section header
        widgets.append(
            cloudwatch.TextWidget(
                markdown="# SQS Queues\nJob submission queue metrics and dead letter queue",
                width=24,
                height=1,
            )
        )

        # Build queue info from regional stacks: (queue_name, dlq_name, region)
        queue_info = [
            (
                regional_stack.job_queue.queue_name,
                regional_stack.job_dlq.queue_name,
                regional_stack.deployment_region,
            )
            for regional_stack in self.regional_stacks
        ]

        # Messages visible and in-flight per region
        messages_widget = cloudwatch.GraphWidget(
            title="SQS - Messages (Visible & In-Flight)",
            left=[
                cloudwatch.Metric(
                    namespace="AWS/SQS",
                    metric_name="ApproximateNumberOfMessagesVisible",
                    dimensions_map={"QueueName": queue_name},
                    statistic="Average",
                    period=Duration.minutes(1),
                    label=f"{region} Visible",
                    region=region,
                )
                for queue_name, _, region in queue_info
            ],
            right=[
                cloudwatch.Metric(
                    namespace="AWS/SQS",
                    metric_name="ApproximateNumberOfMessagesNotVisible",
                    dimensions_map={"QueueName": queue_name},
                    statistic="Average",
                    period=Duration.minutes(1),
                    label=f"{region} In-Flight",
                    region=region,
                )
                for queue_name, _, region in queue_info
            ],
            width=12,
            height=6,
        )
        widgets.append(messages_widget)

        # Age of oldest message (critical for detecting stuck jobs)
        age_widget = cloudwatch.GraphWidget(
            title="SQS - Age of Oldest Message (seconds)",
            left=[
                cloudwatch.Metric(
                    namespace="AWS/SQS",
                    metric_name="ApproximateAgeOfOldestMessage",
                    dimensions_map={"QueueName": queue_name},
                    statistic="Maximum",
                    period=Duration.minutes(1),
                    label=region,
                    region=region,
                )
                for queue_name, _, region in queue_info
            ],
            width=12,
            height=6,
        )
        widgets.append(age_widget)

        # Dead letter queue depth
        dlq_widget = cloudwatch.GraphWidget(
            title="SQS - Dead Letter Queue Depth",
            left=[
                cloudwatch.Metric(
                    namespace="AWS/SQS",
                    metric_name="ApproximateNumberOfMessagesVisible",
                    dimensions_map={"QueueName": dlq_name},
                    statistic="Average",
                    period=Duration.minutes(1),
                    label=f"{region} DLQ",
                    color="#d62728",
                    region=region,
                )
                for _, dlq_name, region in queue_info
            ],
            width=12,
            height=6,
        )
        widgets.append(dlq_widget)

        # Messages sent/received/deleted
        throughput_widget = cloudwatch.GraphWidget(
            title="SQS - Throughput",
            left=[
                cloudwatch.Metric(
                    namespace="AWS/SQS",
                    metric_name="NumberOfMessagesSent",
                    dimensions_map={"QueueName": queue_name},
                    statistic="Sum",
                    period=Duration.minutes(5),
                    label=f"{region} Sent",
                    region=region,
                )
                for queue_name, _, region in queue_info
            ],
            right=[
                cloudwatch.Metric(
                    namespace="AWS/SQS",
                    metric_name="NumberOfMessagesDeleted",
                    dimensions_map={"QueueName": queue_name},
                    statistic="Sum",
                    period=Duration.minutes(5),
                    label=f"{region} Processed",
                    region=region,
                )
                for queue_name, _, region in queue_info
            ],
            width=12,
            height=6,
        )
        widgets.append(throughput_widget)

        return widgets

    def _create_dynamodb_widgets(self) -> list[cloudwatch.IWidget]:
        """Create DynamoDB monitoring widgets for job queue, templates, and webhooks tables."""
        widgets: list[cloudwatch.IWidget] = []

        # Get table names from global stack
        templates_table = self.global_stack.templates_table.table_name
        webhooks_table = self.global_stack.webhooks_table.table_name
        jobs_table = self.global_stack.jobs_table.table_name

        # DynamoDB tables are in the global region
        global_region = self.config.get_global_region()

        # Section header
        widgets.append(
            cloudwatch.TextWidget(
                markdown="# DynamoDB Tables\nJob queue, templates, and webhooks storage metrics",
                width=24,
                height=1,
            )
        )

        # Read/Write capacity consumed
        capacity_widget = cloudwatch.GraphWidget(
            title="DynamoDB - Consumed Capacity",
            left=[
                cloudwatch.Metric(
                    namespace="AWS/DynamoDB",
                    metric_name="ConsumedReadCapacityUnits",
                    dimensions_map={"TableName": jobs_table},
                    statistic="Sum",
                    period=Duration.minutes(5),
                    label="Jobs Read",
                    region=global_region,
                ),
                cloudwatch.Metric(
                    namespace="AWS/DynamoDB",
                    metric_name="ConsumedReadCapacityUnits",
                    dimensions_map={"TableName": templates_table},
                    statistic="Sum",
                    period=Duration.minutes(5),
                    label="Templates Read",
                    region=global_region,
                ),
                cloudwatch.Metric(
                    namespace="AWS/DynamoDB",
                    metric_name="ConsumedReadCapacityUnits",
                    dimensions_map={"TableName": webhooks_table},
                    statistic="Sum",
                    period=Duration.minutes(5),
                    label="Webhooks Read",
                    region=global_region,
                ),
            ],
            right=[
                cloudwatch.Metric(
                    namespace="AWS/DynamoDB",
                    metric_name="ConsumedWriteCapacityUnits",
                    dimensions_map={"TableName": jobs_table},
                    statistic="Sum",
                    period=Duration.minutes(5),
                    label="Jobs Write",
                    region=global_region,
                ),
                cloudwatch.Metric(
                    namespace="AWS/DynamoDB",
                    metric_name="ConsumedWriteCapacityUnits",
                    dimensions_map={"TableName": templates_table},
                    statistic="Sum",
                    period=Duration.minutes(5),
                    label="Templates Write",
                    region=global_region,
                ),
            ],
            width=12,
            height=6,
            region=global_region,
        )
        widgets.append(capacity_widget)

        # Latency metrics
        latency_widget = cloudwatch.GraphWidget(
            title="DynamoDB - Latency (ms)",
            left=[
                cloudwatch.Metric(
                    namespace="AWS/DynamoDB",
                    metric_name="SuccessfulRequestLatency",
                    dimensions_map={"TableName": jobs_table, "Operation": "GetItem"},
                    statistic="Average",
                    period=Duration.minutes(5),
                    label="Jobs GetItem",
                    region=global_region,
                ),
                cloudwatch.Metric(
                    namespace="AWS/DynamoDB",
                    metric_name="SuccessfulRequestLatency",
                    dimensions_map={"TableName": jobs_table, "Operation": "PutItem"},
                    statistic="Average",
                    period=Duration.minutes(5),
                    label="Jobs PutItem",
                    region=global_region,
                ),
                cloudwatch.Metric(
                    namespace="AWS/DynamoDB",
                    metric_name="SuccessfulRequestLatency",
                    dimensions_map={"TableName": jobs_table, "Operation": "Query"},
                    statistic="Average",
                    period=Duration.minutes(5),
                    label="Jobs Query",
                    region=global_region,
                ),
            ],
            width=12,
            height=6,
            region=global_region,
        )
        widgets.append(latency_widget)

        # Throttled requests
        throttle_widget = cloudwatch.GraphWidget(
            title="DynamoDB - Throttled Requests",
            left=[
                cloudwatch.Metric(
                    namespace="AWS/DynamoDB",
                    metric_name="ThrottledRequests",
                    dimensions_map={"TableName": jobs_table},
                    statistic="Sum",
                    period=Duration.minutes(5),
                    label="Jobs",
                    color="#d62728",
                    region=global_region,
                ),
                cloudwatch.Metric(
                    namespace="AWS/DynamoDB",
                    metric_name="ThrottledRequests",
                    dimensions_map={"TableName": templates_table},
                    statistic="Sum",
                    period=Duration.minutes(5),
                    label="Templates",
                    color="#ff7f0e",
                    region=global_region,
                ),
                cloudwatch.Metric(
                    namespace="AWS/DynamoDB",
                    metric_name="ThrottledRequests",
                    dimensions_map={"TableName": webhooks_table},
                    statistic="Sum",
                    period=Duration.minutes(5),
                    label="Webhooks",
                    color="#9467bd",
                    region=global_region,
                ),
            ],
            width=12,
            height=6,
            region=global_region,
        )
        widgets.append(throttle_widget)

        # System errors
        errors_widget = cloudwatch.GraphWidget(
            title="DynamoDB - System Errors",
            left=[
                cloudwatch.Metric(
                    namespace="AWS/DynamoDB",
                    metric_name="SystemErrors",
                    dimensions_map={"TableName": jobs_table},
                    statistic="Sum",
                    period=Duration.minutes(5),
                    label="Jobs",
                    color="#d62728",
                    region=global_region,
                ),
                cloudwatch.Metric(
                    namespace="AWS/DynamoDB",
                    metric_name="SystemErrors",
                    dimensions_map={"TableName": templates_table},
                    statistic="Sum",
                    period=Duration.minutes(5),
                    label="Templates",
                    color="#ff7f0e",
                    region=global_region,
                ),
            ],
            width=12,
            height=6,
            region=global_region,
        )
        widgets.append(errors_widget)

        return widgets

    def _create_eks_widgets(self) -> list[cloudwatch.IWidget]:
        """Create EKS cluster monitoring widgets"""
        widgets: list[cloudwatch.IWidget] = []

        # Section header
        widgets.append(
            cloudwatch.TextWidget(
                markdown="# EKS Clusters\nCluster resource utilization and node metrics",
                width=24,
                height=1,
            )
        )

        # Build cluster info from regional stacks: (cluster_name, region)
        cluster_info = [
            (regional_stack.cluster.cluster_name, regional_stack.deployment_region)
            for regional_stack in self.regional_stacks
        ]

        # EKS cluster status
        cluster_status_widget = cloudwatch.SingleValueWidget(
            title="EKS Clusters - Failed Requests",
            metrics=[
                cloudwatch.Metric(
                    namespace="AWS/EKS",
                    metric_name="cluster_failed_request_count",
                    dimensions_map={"cluster_name": cluster_name},
                    statistic="Sum",
                    period=Duration.minutes(5),
                    region=region,
                )
                for cluster_name, region in cluster_info
            ],
            width=12,
            height=6,
        )
        widgets.append(cluster_status_widget)

        # Container Insights - Node CPU utilization (aggregated across all nodes)
        # Note: region parameter enables cross-region metrics in dashboard
        cpu_widget = cloudwatch.GraphWidget(
            title="EKS Clusters - Node CPU Utilization (%)",
            left=[
                cloudwatch.Metric(
                    namespace="ContainerInsights",
                    metric_name="node_cpu_utilization",
                    dimensions_map={"ClusterName": cluster_name},
                    statistic="Average",
                    period=Duration.minutes(5),
                    label=region,
                    region=region,
                )
                for cluster_name, region in cluster_info
            ],
            width=12,
            height=6,
        )
        widgets.append(cpu_widget)

        # Container Insights - Node Memory utilization (aggregated across all nodes)
        memory_widget = cloudwatch.GraphWidget(
            title="EKS Clusters - Node Memory Utilization (%)",
            left=[
                cloudwatch.Metric(
                    namespace="ContainerInsights",
                    metric_name="node_memory_utilization",
                    dimensions_map={"ClusterName": cluster_name},
                    statistic="Average",
                    period=Duration.minutes(5),
                    label=region,
                    region=region,
                )
                for cluster_name, region in cluster_info
            ],
            width=12,
            height=6,
        )
        widgets.append(memory_widget)

        # Node status - running pods capacity
        node_widget = cloudwatch.GraphWidget(
            title="EKS Clusters - Node Pod Capacity",
            left=[
                cloudwatch.Metric(
                    namespace="ContainerInsights",
                    metric_name="node_status_capacity_pods",
                    dimensions_map={"ClusterName": cluster_name},
                    statistic="Sum",
                    period=Duration.minutes(5),
                    label=f"{region} Capacity",
                    region=region,
                )
                for cluster_name, region in cluster_info
            ],
            right=[
                cloudwatch.Metric(
                    namespace="ContainerInsights",
                    metric_name="node_number_of_running_pods",
                    dimensions_map={"ClusterName": cluster_name},
                    statistic="Sum",
                    period=Duration.minutes(5),
                    label=f"{region} Running",
                    region=region,
                )
                for cluster_name, region in cluster_info
            ],
            width=12,
            height=6,
        )
        widgets.append(node_widget)

        return widgets

    def _create_gpu_widgets(self) -> list[cloudwatch.IWidget]:
        """Create GPU monitoring widgets using DCGM Exporter metrics via ContainerInsights."""
        widgets: list[cloudwatch.IWidget] = []

        widgets.append(
            cloudwatch.TextWidget(
                markdown="# GPU Metrics\nGPU utilization, memory, and temperature from DCGM Exporter",
                width=24,
                height=1,
            )
        )

        cluster_info = [
            (regional_stack.cluster.cluster_name, regional_stack.deployment_region)
            for regional_stack in self.regional_stacks
        ]

        # GPU utilization percentage
        gpu_util_widget = cloudwatch.GraphWidget(
            title="GPU Utilization (%)",
            left=[
                cloudwatch.Metric(
                    namespace="ContainerInsights",
                    metric_name="node_gpu_utilization",
                    dimensions_map={"ClusterName": cluster_name},
                    statistic="Average",
                    period=Duration.minutes(5),
                    label=region,
                    region=region,
                )
                for cluster_name, region in cluster_info
            ],
            width=12,
            height=6,
        )
        widgets.append(gpu_util_widget)

        # GPU memory utilization
        gpu_mem_widget = cloudwatch.GraphWidget(
            title="GPU Memory Utilization (%)",
            left=[
                cloudwatch.Metric(
                    namespace="ContainerInsights",
                    metric_name="node_gpu_memory_utilization",
                    dimensions_map={"ClusterName": cluster_name},
                    statistic="Average",
                    period=Duration.minutes(5),
                    label=region,
                    region=region,
                )
                for cluster_name, region in cluster_info
            ],
            width=12,
            height=6,
        )
        widgets.append(gpu_mem_widget)

        # GPU temperature
        gpu_temp_widget = cloudwatch.GraphWidget(
            title="GPU Temperature (°C)",
            left=[
                cloudwatch.Metric(
                    namespace="ContainerInsights",
                    metric_name="node_gpu_temperature",
                    dimensions_map={"ClusterName": cluster_name},
                    statistic="Maximum",
                    period=Duration.minutes(5),
                    label=region,
                    region=region,
                )
                for cluster_name, region in cluster_info
            ],
            width=12,
            height=6,
        )
        widgets.append(gpu_temp_widget)

        # GPU count (active GPUs)
        gpu_count_widget = cloudwatch.GraphWidget(
            title="Active GPU Count",
            left=[
                cloudwatch.Metric(
                    namespace="ContainerInsights",
                    metric_name="node_gpu_limit",
                    dimensions_map={"ClusterName": cluster_name},
                    statistic="Sum",
                    period=Duration.minutes(5),
                    label=region,
                    region=region,
                )
                for cluster_name, region in cluster_info
            ],
            width=12,
            height=6,
        )
        widgets.append(gpu_count_widget)

        return widgets

    def _create_fsx_widgets(self) -> list[cloudwatch.IWidget]:
        """Create FSx for Lustre monitoring widgets.

        Only emits widgets for regions where the FSx file system is actually
        provisioned (``regional_stack.fsx_file_system`` is non-None). The
        dimension ``FileSystemId`` is the CDK-generated CloudFormation ref
        from each regional stack — CDK's ``cross_region_references=True``
        (enabled on this stack's constructor) plumbs the value across
        regions via SSM + custom resources.

        Returns an empty list if no region has FSx enabled — the dashboard
        skips the section entirely.
        """
        # Collect (file_system_id, region) tuples for regions that have FSx on.
        # fsx_file_system is either a CfnFileSystem or None; the local
        # assignment + is-not-None check lets mypy narrow the type so
        # ``.ref`` access typechecks cleanly (a list comprehension with
        # the guard in the ``if`` clause does not narrow the value clause).
        fsx_info: list[tuple[str, str]] = []
        for regional_stack in self.regional_stacks:
            fsx = getattr(regional_stack, "fsx_file_system", None)
            if fsx is None:
                continue
            fsx_info.append((fsx.ref, regional_stack.deployment_region))
        if not fsx_info:
            return []

        widgets: list[cloudwatch.IWidget] = []

        # Section header
        widgets.append(
            cloudwatch.TextWidget(
                markdown=(
                    "# FSx for Lustre\n"
                    "Parallel file system throughput, IOPS, and free storage "
                    "capacity. Each line below is scoped to the exact GCO "
                    "file system in its region — so unrelated FSx file "
                    "systems in the same account do not appear on the "
                    "dashboard."
                ),
                width=24,
                height=1,
            )
        )

        # Throughput: bytes read vs written
        throughput_widget = cloudwatch.GraphWidget(
            title="FSx - Throughput (Bytes/sec)",
            left=[
                cloudwatch.Metric(
                    namespace="AWS/FSx",
                    metric_name="DataReadBytes",
                    dimensions_map={"FileSystemId": fs_id},
                    statistic="Sum",
                    period=Duration.minutes(1),
                    label=f"{region} Read",
                    region=region,
                )
                for fs_id, region in fsx_info
            ],
            right=[
                cloudwatch.Metric(
                    namespace="AWS/FSx",
                    metric_name="DataWriteBytes",
                    dimensions_map={"FileSystemId": fs_id},
                    statistic="Sum",
                    period=Duration.minutes(1),
                    label=f"{region} Write",
                    region=region,
                )
                for fs_id, region in fsx_info
            ],
            width=12,
            height=6,
        )
        widgets.append(throughput_widget)

        # IOPS: read vs write operations
        iops_widget = cloudwatch.GraphWidget(
            title="FSx - IOPS",
            left=[
                cloudwatch.Metric(
                    namespace="AWS/FSx",
                    metric_name="DataReadOperations",
                    dimensions_map={"FileSystemId": fs_id},
                    statistic="Sum",
                    period=Duration.minutes(1),
                    label=f"{region} Read",
                    region=region,
                )
                for fs_id, region in fsx_info
            ],
            right=[
                cloudwatch.Metric(
                    namespace="AWS/FSx",
                    metric_name="DataWriteOperations",
                    dimensions_map={"FileSystemId": fs_id},
                    statistic="Sum",
                    period=Duration.minutes(1),
                    label=f"{region} Write",
                    region=region,
                )
                for fs_id, region in fsx_info
            ],
            width=12,
            height=6,
        )
        widgets.append(iops_widget)

        # Free storage capacity — the classic "running out of space" signal.
        # FreeDataStorageCapacity is emitted in bytes.
        free_storage_widget = cloudwatch.GraphWidget(
            title="FSx - Free Storage Capacity (Bytes)",
            left=[
                cloudwatch.Metric(
                    namespace="AWS/FSx",
                    metric_name="FreeDataStorageCapacity",
                    dimensions_map={"FileSystemId": fs_id},
                    statistic="Minimum",
                    period=Duration.minutes(5),
                    label=region,
                    region=region,
                )
                for fs_id, region in fsx_info
            ],
            width=24,
            height=6,
        )
        widgets.append(free_storage_widget)

        return widgets

    def _create_valkey_widgets(self) -> list[cloudwatch.IWidget]:
        """Create Valkey (ElastiCache Serverless) monitoring widgets.

        Uses explicit ``clusterId`` dimension values (camelCase — the
        ElastiCache Serverless variant; distinct from the node-based
        ``CacheClusterId``). The regional stack names its cache
        deterministically as ``gco-{deployment_region}``, so we reproduce
        that name here and pin each widget to the exact cache in its
        region. No SEARCH expression, so the dashboard ignores every
        unrelated ElastiCache cluster in the account.
        """
        valkey_enabled = self.config.get_valkey_config().get("enabled", False)
        if not valkey_enabled or not self.regions:
            return []

        widgets: list[cloudwatch.IWidget] = []

        widgets.append(
            cloudwatch.TextWidget(
                markdown=(
                    "# Valkey Serverless Cache\n"
                    "ECPU consumption, storage, hit rate, and request "
                    "latency — scoped to each region's ``gco-{region}`` "
                    "cache exactly (no SEARCH)."
                ),
                width=24,
                height=1,
            )
        )

        # Build (cache_name, region) pairs. cache_name is the literal
        # ``serverless_cache_name`` the regional stack passes to the
        # CfnServerlessCache.
        cache_info = [(f"{self.project_name}-{region}", region) for region in self.regions]

        # ECPU consumption and cache size per region
        for cache_name, region in cache_info:
            widgets.append(
                cloudwatch.GraphWidget(
                    title=f"Valkey - ECPU & Cache Size ({region})",
                    left=[
                        cloudwatch.Metric(
                            namespace="AWS/ElastiCache",
                            metric_name="ElastiCacheProcessingUnits",
                            dimensions_map={"clusterId": cache_name},
                            statistic="Sum",
                            period=Duration.minutes(1),
                            label="ECPUs",
                            region=region,
                        ),
                    ],
                    right=[
                        cloudwatch.Metric(
                            namespace="AWS/ElastiCache",
                            metric_name="BytesUsedForCache",
                            dimensions_map={"clusterId": cache_name},
                            statistic="Average",
                            period=Duration.minutes(5),
                            label="Bytes",
                            region=region,
                        ),
                    ],
                    width=12,
                    height=6,
                    region=region,
                )
            )

        # Hit rate and p99 read/write latency per region
        for cache_name, region in cache_info:
            widgets.append(
                cloudwatch.GraphWidget(
                    title=f"Valkey - Hit Rate & Latency ({region})",
                    left=[
                        cloudwatch.Metric(
                            namespace="AWS/ElastiCache",
                            metric_name="CacheHitRate",
                            dimensions_map={"clusterId": cache_name},
                            statistic="Average",
                            period=Duration.minutes(5),
                            label="Hit Rate %",
                            region=region,
                        ),
                    ],
                    right=[
                        cloudwatch.Metric(
                            namespace="AWS/ElastiCache",
                            metric_name="SuccessfulReadRequestLatency",
                            dimensions_map={"clusterId": cache_name},
                            statistic="p99",
                            period=Duration.minutes(1),
                            label="Read p99 µs",
                            region=region,
                        ),
                        cloudwatch.Metric(
                            namespace="AWS/ElastiCache",
                            metric_name="SuccessfulWriteRequestLatency",
                            dimensions_map={"clusterId": cache_name},
                            statistic="p99",
                            period=Duration.minutes(1),
                            label="Write p99 µs",
                            region=region,
                        ),
                    ],
                    width=12,
                    height=6,
                    region=region,
                )
            )

        return widgets

    def _create_aurora_pgvector_widgets(self) -> list[cloudwatch.IWidget]:
        """Create Aurora Serverless v2 (pgvector) monitoring widgets.

        Pins each widget to the exact Aurora cluster provisioned by the
        regional stack via ``regional_stack.aurora_cluster.cluster_identifier``.
        CDK-generated cluster IDs are CloudFormation tokens; the
        ``cross_region_references=True`` flag on this stack handles
        plumbing them from each regional stack into the monitoring stack
        (us-east-2 by default) through SSM + custom resources.

        Returns an empty list when every region has Aurora pgvector
        disabled so the dashboard skips the section entirely.
        """
        # (cluster_identifier, region) pairs for regions with Aurora on.
        # Use a guarded loop (not a comprehension) so mypy can narrow the
        # Optional[DatabaseCluster] to a real cluster before dereferencing.
        aurora_info: list[tuple[str, str]] = []
        for regional_stack in self.regional_stacks:
            aurora = getattr(regional_stack, "aurora_cluster", None)
            if aurora is None:
                continue
            aurora_info.append((aurora.cluster_identifier, regional_stack.deployment_region))
        if not aurora_info:
            return []

        widgets: list[cloudwatch.IWidget] = []

        widgets.append(
            cloudwatch.TextWidget(
                markdown=(
                    "# Aurora pgvector (Serverless v2)\n"
                    "ACU utilization, database connections, query latency, "
                    "and CPU utilization — pinned to each regional GCO "
                    "Aurora cluster by ID. ACU utilization is the primary "
                    "scale/cost signal for Serverless v2."
                ),
                width=24,
                height=1,
            )
        )

        # ACU utilization and capacity
        for cluster_id, region in aurora_info:
            widgets.append(
                cloudwatch.GraphWidget(
                    title=f"Aurora - ACU Utilization & Capacity ({region})",
                    left=[
                        cloudwatch.Metric(
                            namespace="AWS/RDS",
                            metric_name="ACUUtilization",
                            dimensions_map={"DBClusterIdentifier": cluster_id},
                            statistic="Average",
                            period=Duration.minutes(1),
                            label="ACU %",
                            region=region,
                        ),
                    ],
                    right=[
                        cloudwatch.Metric(
                            namespace="AWS/RDS",
                            metric_name="ServerlessDatabaseCapacity",
                            dimensions_map={"DBClusterIdentifier": cluster_id},
                            statistic="Average",
                            period=Duration.minutes(1),
                            label="ACUs",
                            region=region,
                        ),
                    ],
                    width=12,
                    height=6,
                    region=region,
                )
            )

        # Database connections and CPU utilization
        for cluster_id, region in aurora_info:
            widgets.append(
                cloudwatch.GraphWidget(
                    title=f"Aurora - Connections & CPU ({region})",
                    left=[
                        cloudwatch.Metric(
                            namespace="AWS/RDS",
                            metric_name="DatabaseConnections",
                            dimensions_map={"DBClusterIdentifier": cluster_id},
                            statistic="Average",
                            period=Duration.minutes(1),
                            label="Connections",
                            region=region,
                        ),
                    ],
                    right=[
                        cloudwatch.Metric(
                            namespace="AWS/RDS",
                            metric_name="CPUUtilization",
                            dimensions_map={"DBClusterIdentifier": cluster_id},
                            statistic="Average",
                            period=Duration.minutes(1),
                            label="CPU %",
                            region=region,
                        ),
                    ],
                    width=12,
                    height=6,
                    region=region,
                )
            )

        # Read and write latency p99
        for cluster_id, region in aurora_info:
            widgets.append(
                cloudwatch.GraphWidget(
                    title=f"Aurora - Query Latency p99 ({region})",
                    left=[
                        cloudwatch.Metric(
                            namespace="AWS/RDS",
                            metric_name="ReadLatency",
                            dimensions_map={"DBClusterIdentifier": cluster_id},
                            statistic="p99",
                            period=Duration.minutes(1),
                            label="Read p99",
                            region=region,
                        ),
                    ],
                    right=[
                        cloudwatch.Metric(
                            namespace="AWS/RDS",
                            metric_name="WriteLatency",
                            dimensions_map={"DBClusterIdentifier": cluster_id},
                            statistic="p99",
                            period=Duration.minutes(1),
                            label="Write p99",
                            region=region,
                        ),
                    ],
                    width=24,
                    height=6,
                    region=region,
                )
            )

        return widgets

    def _create_alb_widgets(self) -> list[cloudwatch.IWidget]:
        """Create ALB monitoring widgets scoped to the GCO platform ALB.

        ALBs are created by the AWS Load Balancer Controller at runtime
        from an Ingress resource (not by CDK), so the exact ALB name
        isn't known at synth time. We originally tried reading the ARN
        off the regional stack's ``GaRegistration`` custom resource via
        ``cross_region_references=True``, but that path races the
        custom-resource response pipeline: CDK's cross-region
        ``ExportsWriter`` executes ``Fn::GetAtt: [GaRegistration, AlbArn]``
        before CloudFormation has the updated response data stored, and
        errors with "Vendor response doesn't contain AlbArn attribute".

        Instead we use a SEARCH expression with a composite-token
        filter. The ALB Controller names the platform ALB
        ``k8s-gco-<hash>`` (the namespace is shortened because the
        controller enforces a 32-char total name limit); CloudWatch's
        ``LoadBalancer`` dimension is the ARN suffix ``app/<name>/<hash>``,
        so an unquoted filter ``LoadBalancer=app/k8s-gco-`` performs a
        composite-token match (the sequence ``app``, ``k``, ``8``, ``s``,
        ``gco`` must appear consecutively in the dimension value).
        Double-quoted filters would be exact matches and return nothing
        because no ALB's dimension value is literally ``app/k8s-gco-``.
        """
        widgets: list[cloudwatch.IWidget] = []

        # Section header
        widgets.append(
            cloudwatch.TextWidget(
                markdown=(
                    "# Application Load Balancers\n"
                    "Request metrics, response time, HTTP errors, and "
                    "connection counts — scoped via SEARCH composite-token "
                    "match to ALBs named ``app/k8s-gco-*`` so only the GCO "
                    "platform ALB in each region appears. Inference ALBs "
                    "(named per endpoint) and unrelated ALBs in the "
                    "account are excluded."
                ),
                width=24,
                height=1,
            )
        )

        # Per-region request count
        for region in self.regions:
            widgets.append(
                cloudwatch.GraphWidget(
                    title=f"ALB - Request Count ({region})",
                    left=[
                        cloudwatch.MathExpression(
                            expression=(
                                "SEARCH('{AWS/ApplicationELB,LoadBalancer} "
                                'MetricName="RequestCount" '
                                'LoadBalancer=app/k8s-gco-\', "Sum", 300)'
                            ),
                            label="Request Count",
                            period=Duration.minutes(5),
                        ),
                    ],
                    width=12,
                    height=6,
                    region=region,
                )
            )

        # Per-region response time (average and p99)
        for region in self.regions:
            widgets.append(
                cloudwatch.GraphWidget(
                    title=f"ALB - Response Time ({region})",
                    left=[
                        cloudwatch.MathExpression(
                            expression=(
                                "SEARCH('{AWS/ApplicationELB,LoadBalancer} "
                                'MetricName="TargetResponseTime" '
                                'LoadBalancer=app/k8s-gco-\', "Average", 300)'
                            ),
                            label="Avg Response Time",
                            period=Duration.minutes(5),
                        ),
                        cloudwatch.MathExpression(
                            expression=(
                                "SEARCH('{AWS/ApplicationELB,LoadBalancer} "
                                'MetricName="TargetResponseTime" '
                                'LoadBalancer=app/k8s-gco-\', "p99", 300)'
                            ),
                            label="p99 Response Time",
                            period=Duration.minutes(5),
                        ),
                    ],
                    width=12,
                    height=6,
                    region=region,
                )
            )

        # Per-region HTTP errors (4XX + 5XX from targets)
        for region in self.regions:
            widgets.append(
                cloudwatch.GraphWidget(
                    title=f"ALB - HTTP Errors ({region})",
                    left=[
                        cloudwatch.MathExpression(
                            expression=(
                                "SEARCH('{AWS/ApplicationELB,LoadBalancer} "
                                'MetricName="HTTPCode_Target_4XX_Count" '
                                'LoadBalancer=app/k8s-gco-\', "Sum", 300)'
                            ),
                            label="4XX Errors",
                            period=Duration.minutes(5),
                        ),
                    ],
                    right=[
                        cloudwatch.MathExpression(
                            expression=(
                                "SEARCH('{AWS/ApplicationELB,LoadBalancer} "
                                'MetricName="HTTPCode_Target_5XX_Count" '
                                'LoadBalancer=app/k8s-gco-\', "Sum", 300)'
                            ),
                            label="5XX Errors",
                            period=Duration.minutes(5),
                        ),
                    ],
                    width=12,
                    height=6,
                    region=region,
                )
            )

        # Per-region active connections
        for region in self.regions:
            widgets.append(
                cloudwatch.GraphWidget(
                    title=f"ALB - Active Connections ({region})",
                    left=[
                        cloudwatch.MathExpression(
                            expression=(
                                "SEARCH('{AWS/ApplicationELB,LoadBalancer} "
                                'MetricName="ActiveConnectionCount" '
                                'LoadBalancer=app/k8s-gco-\', "Sum", 300)'
                            ),
                            label="Active Connections",
                            period=Duration.minutes(5),
                        ),
                    ],
                    width=12,
                    height=6,
                    region=region,
                )
            )

        return widgets

    def _create_application_widgets(self) -> list[cloudwatch.IWidget]:
        """Create custom application monitoring widgets"""
        widgets: list[cloudwatch.IWidget] = []

        # Section header
        widgets.append(
            cloudwatch.TextWidget(
                markdown="# Application Metrics\n"
                "Health monitor and manifest processor metrics. "
                "Application logs are available in Container Insights at "
                "`/aws/containerinsights/<cluster>/application`.",
                width=24,
                height=1,
            )
        )

        # Build cluster info from regional stacks: (cluster_name, region)
        cluster_info = [
            (regional_stack.cluster.cluster_name, regional_stack.deployment_region)
            for regional_stack in self.regional_stacks
        ]

        # Health monitor metrics
        health_monitor_widget = cloudwatch.GraphWidget(
            title="Health Monitor - Resource Utilization",
            left=[
                cloudwatch.Metric(
                    namespace="GCO/HealthMonitor",
                    metric_name="ClusterCpuUtilization",
                    dimensions_map={
                        "ClusterName": cluster_name,
                        "Region": region,
                    },
                    statistic="Average",
                    period=Duration.minutes(5),
                    label=f"{region} CPU",
                    region=region,
                )
                for cluster_name, region in cluster_info
            ],
            right=[
                cloudwatch.Metric(
                    namespace="GCO/HealthMonitor",
                    metric_name="ClusterMemoryUtilization",
                    dimensions_map={
                        "ClusterName": cluster_name,
                        "Region": region,
                    },
                    statistic="Average",
                    period=Duration.minutes(5),
                    label=f"{region} Memory",
                    region=region,
                )
                for cluster_name, region in cluster_info
            ],
            width=12,
            height=6,
        )
        widgets.append(health_monitor_widget)

        # Manifest processor metrics
        manifest_processor_widget = cloudwatch.GraphWidget(
            title="Manifest Processor - Submissions",
            left=[
                cloudwatch.Metric(
                    namespace="GCO/ManifestProcessor",
                    metric_name="ManifestSubmissions",
                    dimensions_map={
                        "ClusterName": cluster_name,
                        "Region": region,
                    },
                    statistic="Sum",
                    period=Duration.minutes(5),
                    label=f"{region} Submissions",
                    region=region,
                )
                for cluster_name, region in cluster_info
            ],
            right=[
                cloudwatch.Metric(
                    namespace="GCO/ManifestProcessor",
                    metric_name="ManifestFailures",
                    dimensions_map={
                        "ClusterName": cluster_name,
                        "Region": region,
                    },
                    statistic="Sum",
                    period=Duration.minutes(5),
                    label=f"{region} Failures",
                    color="#d62728",
                    region=region,
                )
                for cluster_name, region in cluster_info
            ],
            width=12,
            height=6,
        )
        widgets.append(manifest_processor_widget)

        # Container Insights - Pod restarts (indicates application issues)
        pod_restarts_widget = cloudwatch.GraphWidget(
            title="Container Insights - Pod Restarts",
            left=[
                cloudwatch.Metric(
                    namespace="ContainerInsights",
                    metric_name="pod_number_of_container_restarts",
                    dimensions_map={"ClusterName": cluster_name},
                    statistic="Sum",
                    period=Duration.minutes(5),
                    label=f"{region}",
                    region=region,
                )
                for cluster_name, region in cluster_info
            ],
            width=12,
            height=6,
        )
        widgets.append(pod_restarts_widget)

        # Secret rotation Lambda metrics (Secrets Manager doesn't publish rotation metrics,
        # so we monitor the rotation Lambda function instead)
        if self.api_gateway_stack:
            rotation_function_name = self.api_gateway_stack.rotation_lambda.function_name
            api_gw_region = self.config.get_api_gateway_region()

            rotation_widget = cloudwatch.GraphWidget(
                title="Secret Rotation Lambda - Invocations & Errors",
                left=[
                    cloudwatch.Metric(
                        namespace="AWS/Lambda",
                        metric_name="Invocations",
                        dimensions_map={"FunctionName": rotation_function_name},
                        statistic="Sum",
                        period=Duration.hours(1),
                        label="Invocations",
                        color="#2ca02c",
                        region=api_gw_region,
                    ),
                ],
                right=[
                    cloudwatch.Metric(
                        namespace="AWS/Lambda",
                        metric_name="Errors",
                        dimensions_map={"FunctionName": rotation_function_name},
                        statistic="Sum",
                        period=Duration.hours(1),
                        label="Errors",
                        color="#d62728",
                        region=api_gw_region,
                    ),
                ],
                width=12,
                height=6,
            )
            widgets.append(rotation_widget)
        else:
            # Fallback text widget if api_gateway_stack not available
            fallback_widget = cloudwatch.TextWidget(
                markdown="**Secret Rotation:** API Gateway stack not configured. "
                "Rotation Lambda metrics unavailable.",
                width=12,
                height=6,
            )
            widgets.append(fallback_widget)

        return widgets

    def _create_alarms(self) -> None:
        """Create CloudWatch alarms"""
        self._create_global_accelerator_alarms()
        self._create_api_gateway_alarms()
        self._create_lambda_alarms()
        self._create_sqs_alarms()
        self._create_dynamodb_alarms()
        self._create_eks_alarms()
        self._create_alb_alarms()
        self._create_application_alarms()

    def _create_global_accelerator_alarms(self) -> None:
        """Create Global Accelerator alarms.

        Note: Global Accelerator metrics are only available in us-west-2.
        CloudWatch Alarms must be in the same region as the metrics they monitor.
        Since this monitoring stack may be deployed in a different region,
        we skip GA alarms here. To monitor GA, either:
        1. Create alarms manually in us-west-2
        2. Use CloudWatch cross-region dashboard widgets (which we do)
        3. Deploy a separate alarm stack in us-west-2
        """
        # GA alarms skipped - metrics only available in us-west-2
        # Dashboard widgets use region parameter to display GA metrics correctly
        pass

    def _create_api_gateway_alarms(self) -> None:
        """Create API Gateway alarms"""
        # Get the actual API name from the api_gateway_stack
        api_name = (
            self.api_gateway_stack.api.rest_api_name
            if self.api_gateway_stack
            else f"{self.project_name}-global-api"
        )

        # High 5XX error rate
        api_5xx_alarm = cloudwatch.Alarm(
            self,
            "ApiGateway5xxAlarm",
            alarm_description="API Gateway has high 5XX error rate",
            metric=cloudwatch.Metric(
                namespace="AWS/ApiGateway",
                metric_name="5XXError",
                dimensions_map={"ApiName": api_name},
                statistic="Sum",
                period=Duration.minutes(5),
            ),
            threshold=10,
            comparison_operator=cloudwatch.ComparisonOperator.GREATER_THAN_THRESHOLD,
            evaluation_periods=2,
            datapoints_to_alarm=2,
            treat_missing_data=cloudwatch.TreatMissingData.NOT_BREACHING,
        )
        api_5xx_alarm.add_alarm_action(cw_actions.SnsAction(self.alert_topic))

        # High latency
        api_latency_alarm = cloudwatch.Alarm(
            self,
            "ApiGatewayHighLatencyAlarm",
            alarm_description="API Gateway has high latency",
            metric=cloudwatch.Metric(
                namespace="AWS/ApiGateway",
                metric_name="Latency",
                dimensions_map={"ApiName": api_name},
                statistic="p99",
                period=Duration.minutes(5),
            ),
            threshold=10000,  # 10 seconds
            comparison_operator=cloudwatch.ComparisonOperator.GREATER_THAN_THRESHOLD,
            evaluation_periods=3,
            datapoints_to_alarm=2,
            treat_missing_data=cloudwatch.TreatMissingData.NOT_BREACHING,
        )
        api_latency_alarm.add_alarm_action(cw_actions.SnsAction(self.alert_topic))

    def _create_lambda_alarms(self) -> None:
        """Create Lambda function alarms."""
        if self.api_gateway_stack is None:
            return

        proxy_lambda = self.api_gateway_stack.proxy_lambda
        if proxy_lambda is not None:
            proxy_function_name = proxy_lambda.function_name
            proxy_errors_alarm = cloudwatch.Alarm(
                self,
                "ProxyLambdaErrorsAlarm",
                alarm_description="API Gateway proxy Lambda has errors",
                metric=cloudwatch.Metric(
                    namespace="AWS/Lambda",
                    metric_name="Errors",
                    dimensions_map={"FunctionName": proxy_function_name},
                    statistic="Sum",
                    period=Duration.minutes(5),
                ),
                threshold=5,
                comparison_operator=cloudwatch.ComparisonOperator.GREATER_THAN_THRESHOLD,
                evaluation_periods=2,
                datapoints_to_alarm=2,
                treat_missing_data=cloudwatch.TreatMissingData.NOT_BREACHING,
            )
            proxy_errors_alarm.add_alarm_action(cw_actions.SnsAction(self.alert_topic))

            proxy_throttles_alarm = cloudwatch.Alarm(
                self,
                "ProxyLambdaThrottlesAlarm",
                alarm_description="API Gateway proxy Lambda is being throttled",
                metric=cloudwatch.Metric(
                    namespace="AWS/Lambda",
                    metric_name="Throttles",
                    dimensions_map={"FunctionName": proxy_function_name},
                    statistic="Sum",
                    period=Duration.minutes(5),
                ),
                threshold=1,
                comparison_operator=cloudwatch.ComparisonOperator.GREATER_THAN_OR_EQUAL_TO_THRESHOLD,
                evaluation_periods=2,
                datapoints_to_alarm=2,
                treat_missing_data=cloudwatch.TreatMissingData.NOT_BREACHING,
            )
            proxy_throttles_alarm.add_alarm_action(cw_actions.SnsAction(self.alert_topic))

        rotation_errors_alarm = cloudwatch.Alarm(
            self,
            "RotationLambdaErrorsAlarm",
            alarm_description="Secret rotation Lambda has errors",
            metric=cloudwatch.Metric(
                namespace="AWS/Lambda",
                metric_name="Errors",
                dimensions_map={
                    "FunctionName": self.api_gateway_stack.rotation_lambda.function_name
                },
                statistic="Sum",
                period=Duration.hours(1),
            ),
            threshold=1,
            comparison_operator=cloudwatch.ComparisonOperator.GREATER_THAN_OR_EQUAL_TO_THRESHOLD,
            evaluation_periods=1,
            datapoints_to_alarm=1,
            treat_missing_data=cloudwatch.TreatMissingData.NOT_BREACHING,
        )
        rotation_errors_alarm.add_alarm_action(cw_actions.SnsAction(self.alert_topic))

    def _create_sqs_alarms(self) -> None:
        """Create SQS queue alarms"""
        for regional_stack in self.regional_stacks:
            region = regional_stack.deployment_region
            queue_name = regional_stack.job_queue.queue_name
            dlq_name = regional_stack.job_dlq.queue_name
            region_id = region.replace("-", "").title()

            # Old message alarm (stuck jobs)
            old_message_alarm = cloudwatch.Alarm(
                self,
                f"SqsOldMessageAlarm{region_id}",
                alarm_description=f"SQS queue in {region} has old messages (potential stuck jobs)",
                metric=cloudwatch.Metric(
                    namespace="AWS/SQS",
                    metric_name="ApproximateAgeOfOldestMessage",
                    dimensions_map={"QueueName": queue_name},
                    statistic="Maximum",
                    period=Duration.minutes(5),
                ),
                threshold=3600,  # 1 hour
                comparison_operator=cloudwatch.ComparisonOperator.GREATER_THAN_THRESHOLD,
                evaluation_periods=2,
                datapoints_to_alarm=2,
                treat_missing_data=cloudwatch.TreatMissingData.NOT_BREACHING,
            )
            old_message_alarm.add_alarm_action(cw_actions.SnsAction(self.alert_topic))

            # Dead letter queue alarm
            dlq_alarm = cloudwatch.Alarm(
                self,
                f"SqsDlqAlarm{region_id}",
                alarm_description=f"SQS dead letter queue in {region} has messages",
                metric=cloudwatch.Metric(
                    namespace="AWS/SQS",
                    metric_name="ApproximateNumberOfMessagesVisible",
                    dimensions_map={"QueueName": dlq_name},
                    statistic="Sum",
                    period=Duration.minutes(5),
                ),
                threshold=1,
                comparison_operator=cloudwatch.ComparisonOperator.GREATER_THAN_OR_EQUAL_TO_THRESHOLD,
                evaluation_periods=1,
                datapoints_to_alarm=1,
                treat_missing_data=cloudwatch.TreatMissingData.NOT_BREACHING,
            )
            dlq_alarm.add_alarm_action(cw_actions.SnsAction(self.alert_topic))

    def _create_dynamodb_alarms(self) -> None:
        """Create DynamoDB alarms for job queue, templates, and webhooks tables."""
        # Get table names from global stack
        jobs_table = self.global_stack.jobs_table.table_name

        # DynamoDB tables are in the global region
        global_region = self.config.get_global_region()

        # Jobs table throttling alarm
        jobs_throttle_alarm = cloudwatch.Alarm(
            self,
            "DynamoDBJobsThrottleAlarm",
            alarm_description="DynamoDB jobs table is being throttled",
            metric=cloudwatch.Metric(
                namespace="AWS/DynamoDB",
                metric_name="ThrottledRequests",
                dimensions_map={"TableName": jobs_table},
                statistic="Sum",
                period=Duration.minutes(5),
                region=global_region,
            ),
            threshold=1,
            comparison_operator=cloudwatch.ComparisonOperator.GREATER_THAN_OR_EQUAL_TO_THRESHOLD,
            evaluation_periods=2,
            datapoints_to_alarm=2,
            treat_missing_data=cloudwatch.TreatMissingData.NOT_BREACHING,
        )
        jobs_throttle_alarm.add_alarm_action(cw_actions.SnsAction(self.alert_topic))

        # Jobs table system errors alarm
        jobs_errors_alarm = cloudwatch.Alarm(
            self,
            "DynamoDBJobsErrorsAlarm",
            alarm_description="DynamoDB jobs table has system errors",
            metric=cloudwatch.Metric(
                namespace="AWS/DynamoDB",
                metric_name="SystemErrors",
                dimensions_map={"TableName": jobs_table},
                statistic="Sum",
                period=Duration.minutes(5),
                region=global_region,
            ),
            threshold=1,
            comparison_operator=cloudwatch.ComparisonOperator.GREATER_THAN_OR_EQUAL_TO_THRESHOLD,
            evaluation_periods=1,
            datapoints_to_alarm=1,
            treat_missing_data=cloudwatch.TreatMissingData.NOT_BREACHING,
        )
        jobs_errors_alarm.add_alarm_action(cw_actions.SnsAction(self.alert_topic))

    def _create_eks_alarms(self) -> None:
        """Create EKS cluster alarms"""
        for regional_stack in self.regional_stacks:
            region = regional_stack.deployment_region
            cluster_name = regional_stack.cluster.cluster_name
            region_id = region.replace("-", "").title()

            # High CPU utilization alarm (node-level metric)
            high_cpu_alarm = cloudwatch.Alarm(
                self,
                f"EksHighCpuAlarm{region_id}",
                alarm_description=f"EKS cluster {cluster_name} has high CPU utilization",
                metric=cloudwatch.Metric(
                    namespace="ContainerInsights",
                    metric_name="node_cpu_utilization",
                    dimensions_map={"ClusterName": cluster_name},
                    statistic="Average",
                    period=Duration.minutes(5),
                ),
                threshold=80,
                comparison_operator=cloudwatch.ComparisonOperator.GREATER_THAN_THRESHOLD,
                evaluation_periods=3,
                datapoints_to_alarm=2,
                treat_missing_data=cloudwatch.TreatMissingData.NOT_BREACHING,
            )
            high_cpu_alarm.add_alarm_action(cw_actions.SnsAction(self.alert_topic))

            # High memory utilization alarm (node-level metric)
            high_memory_alarm = cloudwatch.Alarm(
                self,
                f"EksHighMemoryAlarm{region_id}",
                alarm_description=f"EKS cluster {cluster_name} has high memory utilization",
                metric=cloudwatch.Metric(
                    namespace="ContainerInsights",
                    metric_name="node_memory_utilization",
                    dimensions_map={"ClusterName": cluster_name},
                    statistic="Average",
                    period=Duration.minutes(5),
                ),
                threshold=85,
                comparison_operator=cloudwatch.ComparisonOperator.GREATER_THAN_THRESHOLD,
                evaluation_periods=3,
                datapoints_to_alarm=2,
                treat_missing_data=cloudwatch.TreatMissingData.NOT_BREACHING,
            )
            high_memory_alarm.add_alarm_action(cw_actions.SnsAction(self.alert_topic))

    def _create_alb_alarms(self) -> None:
        """Create ALB alarms.

        Status: no alarms created yet, even though we now have the ALB
        ARN at deploy time via the GA registration custom resource (which
        also feeds the dashboard widgets). Adding per-ALB alarms here is
        a straightforward enhancement — derive the ``LoadBalancer``
        dimension the same way ``_create_alb_widgets`` does
        (``Fn.split(":loadbalancer/", alb_arn)[1]``) and wire it into
        ``cloudwatch.Alarm`` constructs.

        For now we rely on:
        1. Dashboard widgets pinned to each platform ALB (see
           ``_create_alb_widgets``)
        2. EKS Container Insights alarms for pod/node health
        3. API Gateway alarms for request-level monitoring
        """
        # TODO: Add UnHealthyHostCount / 5XXCount alarms using the ARN
        # returned by regional_stack.ga_registration.get_att_string("AlbArn").
        # The test suite explicitly documents that the ALB alarm count is
        # currently zero (test_alb_unhealthy_hosts_alarm_skipped); update
        # that test when adding real alarms.
        pass

    def _create_application_alarms(self) -> None:
        """Create application-specific alarms"""
        for regional_stack in self.regional_stacks:
            region = regional_stack.deployment_region
            cluster_name = regional_stack.cluster.cluster_name
            region_id = region.replace("-", "").title()

            # High manifest failure rate alarm
            high_failure_rate_alarm = cloudwatch.Alarm(
                self,
                f"ManifestHighFailureRateAlarm{region_id}",
                alarm_description=f"Manifest processor in {region} has high failure rate",
                metric=cloudwatch.Metric(
                    namespace="GCO/ManifestProcessor",
                    metric_name="ManifestFailures",
                    dimensions_map={"ClusterName": cluster_name, "Region": region},
                    statistic="Sum",
                    period=Duration.minutes(5),
                ),
                threshold=10,
                comparison_operator=cloudwatch.ComparisonOperator.GREATER_THAN_THRESHOLD,
                evaluation_periods=2,
                datapoints_to_alarm=2,
                treat_missing_data=cloudwatch.TreatMissingData.NOT_BREACHING,
            )
            high_failure_rate_alarm.add_alarm_action(cw_actions.SnsAction(self.alert_topic))

    def _create_composite_alarms(self) -> None:
        """Create composite alarms for better signal-to-noise ratio"""

        # Store individual alarms for composite alarm references
        regional_alarms: dict[str, list[cloudwatch.Alarm]] = {}

        for regional_stack in self.regional_stacks:
            region = regional_stack.deployment_region
            cluster_name = regional_stack.cluster.cluster_name
            region_id = region.replace("-", "").title()
            regional_alarms[region] = []

            # Create regional health composite alarm
            # Triggers when multiple issues occur in the same region
            eks_cpu_alarm = cloudwatch.Alarm(
                self,
                f"CompositeEksCpu{region_id}",
                metric=cloudwatch.Metric(
                    namespace="ContainerInsights",
                    metric_name="node_cpu_utilization",
                    dimensions_map={"ClusterName": cluster_name},
                    statistic="Average",
                    period=Duration.minutes(5),
                ),
                threshold=90,
                comparison_operator=cloudwatch.ComparisonOperator.GREATER_THAN_THRESHOLD,
                evaluation_periods=2,
                treat_missing_data=cloudwatch.TreatMissingData.NOT_BREACHING,
            )
            regional_alarms[region].append(eks_cpu_alarm)

            eks_memory_alarm = cloudwatch.Alarm(
                self,
                f"CompositeEksMemory{region_id}",
                metric=cloudwatch.Metric(
                    namespace="ContainerInsights",
                    metric_name="node_memory_utilization",
                    dimensions_map={"ClusterName": cluster_name},
                    statistic="Average",
                    period=Duration.minutes(5),
                ),
                threshold=90,
                comparison_operator=cloudwatch.ComparisonOperator.GREATER_THAN_THRESHOLD,
                evaluation_periods=2,
                treat_missing_data=cloudwatch.TreatMissingData.NOT_BREACHING,
            )
            regional_alarms[region].append(eks_memory_alarm)

        # Create composite alarm for critical regional issues
        for region, alarms in regional_alarms.items():
            region_id = region.replace("-", "").title()
            if len(alarms) >= 2:
                composite_alarm = cloudwatch.CompositeAlarm(
                    self,
                    f"RegionalCriticalAlarm{region_id}",
                    alarm_description=f"Critical: Multiple issues detected in {region}",
                    alarm_rule=cloudwatch.AlarmRule.all_of(*alarms),
                )
                composite_alarm.add_alarm_action(cw_actions.SnsAction(self.alert_topic))

        # API Gateway + Lambda composite alarm (only if api_gateway_stack is available)
        if self.api_gateway_stack and self.api_gateway_stack.proxy_lambda is not None:
            api_name = self.api_gateway_stack.api.rest_api_name
            proxy_function_name = self.api_gateway_stack.proxy_lambda.function_name

            api_error_alarm = cloudwatch.Alarm(
                self,
                "CompositeApiErrors",
                metric=cloudwatch.Metric(
                    namespace="AWS/ApiGateway",
                    metric_name="5XXError",
                    dimensions_map={"ApiName": api_name},
                    statistic="Sum",
                    period=Duration.minutes(5),
                ),
                threshold=5,
                comparison_operator=cloudwatch.ComparisonOperator.GREATER_THAN_THRESHOLD,
                evaluation_periods=2,
                treat_missing_data=cloudwatch.TreatMissingData.NOT_BREACHING,
            )

            lambda_error_alarm = cloudwatch.Alarm(
                self,
                "CompositeLambdaErrors",
                metric=cloudwatch.Metric(
                    namespace="AWS/Lambda",
                    metric_name="Errors",
                    dimensions_map={"FunctionName": proxy_function_name},
                    statistic="Sum",
                    period=Duration.minutes(5),
                ),
                threshold=3,
                comparison_operator=cloudwatch.ComparisonOperator.GREATER_THAN_THRESHOLD,
                evaluation_periods=2,
                treat_missing_data=cloudwatch.TreatMissingData.NOT_BREACHING,
            )

            api_lambda_composite = cloudwatch.CompositeAlarm(
                self,
                "ApiLambdaCompositeAlarm",
                alarm_description="Critical: Both API Gateway and Lambda proxy have errors",
                alarm_rule=cloudwatch.AlarmRule.all_of(api_error_alarm, lambda_error_alarm),
            )
            api_lambda_composite.add_alarm_action(cw_actions.SnsAction(self.alert_topic))

    def _create_custom_metrics(self) -> None:
        """Create custom metric filters and log groups"""
        for regional_stack in self.regional_stacks:
            region = regional_stack.deployment_region
            region_id = region.replace("-", "").title()

            # Health monitor log group
            # log_group_name intentionally omitted - let CDK generate unique name
            logs.LogGroup(
                self,
                f"HealthMonitorLogGroup{region_id}",
                retention=logs.RetentionDays.ONE_MONTH,
                removal_policy=RemovalPolicy.DESTROY,
            )

            # Manifest processor log group
            # log_group_name intentionally omitted - let CDK generate unique name
            logs.LogGroup(
                self,
                f"ManifestProcessorLogGroup{region_id}",
                retention=logs.RetentionDays.ONE_MONTH,
                removal_policy=RemovalPolicy.DESTROY,
            )

    def _create_outputs(self) -> None:
        """Create CloudFormation outputs"""
        CfnOutput(
            self,
            "DashboardUrl",
            value=f"https://console.aws.amazon.com/cloudwatch/home?region={self.region}#dashboards:name={self.dashboard.dashboard_name}",
            description="CloudWatch Dashboard URL",
        )

        CfnOutput(
            self,
            "AlertTopicArn",
            value=self.alert_topic.topic_arn,
            description="SNS Topic ARN for monitoring alerts",
        )

        CfnOutput(
            self,
            "AlarmCount",
            value="See CloudWatch Alarms console for full list",
            description="Monitoring alarms created",
        )
