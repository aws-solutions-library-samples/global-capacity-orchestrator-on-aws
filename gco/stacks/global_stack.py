"""
Global stack for GCO (Global Capacity Orchestrator on AWS) - AWS Global Accelerator configuration.

This stack creates the global-level resources that span all regions:
- AWS Global Accelerator with TCP listeners on ports 80 and 443
- Endpoint groups for each configured region
- SSM parameters for cross-region endpoint group ARN sharing
- DynamoDB tables for templates and webhooks (global, replicated)

The Global Accelerator provides:
- Single global endpoint for all regions
- Automatic health-based routing to nearest healthy region
- DDoS protection via AWS Shield Standard
- Reduced latency through AWS global network

Architecture:
    Global Accelerator → Listener (80, 443) → Endpoint Groups (per region)
                                                    ↓
                                            Regional ALBs (registered separately)
"""

from typing import Any

from aws_cdk import (
    CfnOutput,
    Duration,
    Fn,
    RemovalPolicy,
    Stack,
)
from aws_cdk import aws_backup as backup
from aws_cdk import aws_dynamodb as dynamodb
from aws_cdk import aws_ecr as ecr
from aws_cdk import aws_events as events
from aws_cdk import aws_globalaccelerator as ga
from aws_cdk import aws_iam as iam
from aws_cdk import aws_kms as kms
from aws_cdk import aws_lambda as lambda_
from aws_cdk import aws_s3 as s3
from aws_cdk import aws_ssm as ssm
from constructs import Construct

from gco.config.config_loader import ConfigLoader
from gco.stacks.constants import (
    LAMBDA_PYTHON_RUNTIME,
    cluster_shared_bucket_name_prefix,
    cluster_shared_ssm_parameter_prefix,
)

# <pyflowchart-code-diagram> BEGIN - auto-inserted, do not edit
# Flowchart(s) generated from this file:
#   * ``GCOGlobalStack.__init__`` -> ``diagrams/code_diagrams/gco/stacks/global_stack.GCOGlobalStack___init__.html``
#     (PNG: ``diagrams/code_diagrams/gco/stacks/global_stack.GCOGlobalStack___init__.png``)
# Regenerate with ``python diagrams/code_diagrams/generate.py``.
# <pyflowchart-code-diagram> END


# Default values for the ``images`` cdk.json block. The defaults match the
# documented retention posture: repos survive a stack destroy by default
# (``retain``), non-empty repos block destroy unless the operator explicitly
# flips ``empty_on_delete`` to true, lifecycle keeps the latest 20 tagged
# images and expires untagged ones after 7 days, and replication is enabled
# by default to every deployed region.
_IMAGES_DEFAULT_REMOVAL_POLICY = "retain"
_IMAGES_DEFAULT_EMPTY_ON_DELETE = False
_IMAGES_DEFAULT_KEEP_TAGGED = 20
_IMAGES_DEFAULT_EXPIRE_UNTAGGED_DAYS = 7
_IMAGES_DEFAULT_REPLICATION_ENABLED = True
_IMAGES_DEFAULT_REPLICATION_DESTINATIONS = "all_deployed_regions"

_IMAGES_VALID_REMOVAL_POLICIES = ("retain", "destroy")


def _parse_images_config(cdk_context: dict[str, Any] | None) -> dict[str, Any]:
    """Parse the ``images`` block from cdk.json with defaults applied.

    Returns a normalized dict shape that the rest of the global stack
    can consume without re-parsing. Validates ``removal_policy`` against
    the set ``{"retain", "destroy"}`` and ``replication.destinations``
    against either the literal string ``"all_deployed_regions"`` or a
    ``list[str]``.

    Args:
        cdk_context: The dict returned by ``self.node.try_get_context("images")``.
            ``None`` (the key being absent) is equivalent to an empty dict.

    Returns:
        A dict with keys ``removal_policy``, ``empty_on_delete``,
        ``lifecycle`` (with ``keep_tagged`` and ``expire_untagged_days``),
        and ``replication`` (with ``enabled`` and ``destinations``).
    """
    raw = cdk_context or {}

    removal_policy = raw.get("removal_policy", _IMAGES_DEFAULT_REMOVAL_POLICY)
    if not isinstance(removal_policy, str) or removal_policy not in _IMAGES_VALID_REMOVAL_POLICIES:
        raise ValueError(
            f"images.removal_policy must be 'retain' or 'destroy', got {removal_policy!r}"
        )

    empty_on_delete = bool(raw.get("empty_on_delete", _IMAGES_DEFAULT_EMPTY_ON_DELETE))

    lifecycle_raw = raw.get("lifecycle") or {}
    if not isinstance(lifecycle_raw, dict):
        raise ValueError(f"images.lifecycle must be a mapping, got {type(lifecycle_raw).__name__}")
    keep_tagged = int(lifecycle_raw.get("keep_tagged", _IMAGES_DEFAULT_KEEP_TAGGED))
    expire_untagged_days = int(
        lifecycle_raw.get("expire_untagged_days", _IMAGES_DEFAULT_EXPIRE_UNTAGGED_DAYS)
    )

    replication_raw = raw.get("replication") or {}
    if not isinstance(replication_raw, dict):
        raise ValueError(
            f"images.replication must be a mapping, got {type(replication_raw).__name__}"
        )
    replication_enabled = bool(replication_raw.get("enabled", _IMAGES_DEFAULT_REPLICATION_ENABLED))
    destinations = replication_raw.get("destinations", _IMAGES_DEFAULT_REPLICATION_DESTINATIONS)
    if isinstance(destinations, str):
        if destinations != _IMAGES_DEFAULT_REPLICATION_DESTINATIONS:
            raise ValueError(
                "images.replication.destinations must be the string "
                f"{_IMAGES_DEFAULT_REPLICATION_DESTINATIONS!r} or a list of region names, "
                f"got {destinations!r}"
            )
    elif isinstance(destinations, list):
        if not all(isinstance(item, str) for item in destinations):
            raise ValueError(
                "images.replication.destinations list must contain only region name strings"
            )
    else:
        raise ValueError(
            "images.replication.destinations must be the string "
            f"{_IMAGES_DEFAULT_REPLICATION_DESTINATIONS!r} or a list of region names, "
            f"got {type(destinations).__name__}"
        )

    return {
        "removal_policy": removal_policy,
        "empty_on_delete": empty_on_delete,
        "lifecycle": {
            "keep_tagged": keep_tagged,
            "expire_untagged_days": expire_untagged_days,
        },
        "replication": {
            "enabled": replication_enabled,
            "destinations": destinations,
        },
    }


class GCOGlobalStack(Stack):
    """
    Global resources stack including AWS Global Accelerator.

    This stack must be deployed before regional stacks. Regional stacks
    will register their ALBs with the endpoint groups created here.

    Attributes:
        accelerator: The Global Accelerator resource
        listener: TCP listener for HTTP/HTTPS traffic
        endpoint_groups: Dict mapping region names to endpoint groups
        templates_table: DynamoDB table for job templates
        webhooks_table: DynamoDB table for webhooks
        missions_table: DynamoDB table for mission session state
    """

    def __init__(
        self, scope: Construct, construct_id: str, config: ConfigLoader, **kwargs: Any
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)

        self.config = config
        self.regional_endpoints: dict[str, str] = {}
        self.endpoint_groups: dict[str, ga.EndpointGroup] = {}

        ga_config = self.config.get_global_accelerator_config()

        # Store the accelerator name for reference by other stacks
        self.accelerator_name = ga_config["name"]

        # Create DynamoDB tables for templates and webhooks
        self._create_dynamodb_tables()

        # Create S3 bucket for model weights
        self._create_model_bucket()

        # Create always-on Cluster_Shared_Bucket + KMS key + SSM parameters.
        # These run unconditionally (no feature toggle) — they are consumed by
        # every Regional_Stack and, when analytics is enabled, by GCOAnalyticsStack.
        self._create_cluster_shared_kms_key()
        self._create_cluster_shared_bucket()
        self._publish_cluster_shared_bucket_ssm_params()

        # Create AWS Backup plan for DynamoDB tables
        self._create_backup_plan()

        # Container image registry — parses the cdk.json ``images`` block,
        # provisions the optional ECR replication rule for ``gco/*`` repos,
        # and creates the lookup-or-create custom resource Lambda that
        # ``cli images init`` will invoke per-repo on demand. The Lambda
        # construct is created here regardless of replication settings so
        # the function ARN is available for downstream invocations.
        self.images_config = _parse_images_config(self.node.try_get_context("images"))
        self._create_image_replication_rule()
        self._create_image_lookup_lambda()

        # Optional Historical Capacity Surface add-on (gated by historical.enabled
        # in cdk.json). Folded into the global stack rather than a separate stack
        # so it reuses the global DynamoDB/encryption conventions.
        if self.config.get_capacity_history_enabled():
            self._create_capacity_poller()

        # Create Global Accelerator with TCP protocol for HTTP/HTTPS traffic
        self.accelerator = ga.Accelerator(
            self, "GCOAccelerator", accelerator_name=self.accelerator_name, enabled=True
        )

        # Store the accelerator ID for CloudWatch metrics
        # CloudWatch uses the accelerator ID (UUID), not the name or ARN
        # ARN format: arn:aws:globalaccelerator::<account>:accelerator/<accelerator-id>
        # Use Fn.select and Fn.split to extract the ID at deploy time
        self.accelerator_id = Fn.select(1, Fn.split("/", self.accelerator.accelerator_arn))

        # Create listener for both HTTP (80) and HTTPS (443) traffic.
        # Client affinity controls whether GA pins a client to the same
        # endpoint across connections (see ``_resolve_client_affinity``).
        self.listener = self.accelerator.add_listener(
            "GCOListener",
            port_ranges=[
                ga.PortRange(from_port=80, to_port=80),
                ga.PortRange(from_port=443, to_port=443),
            ],
            protocol=ga.ConnectionProtocol.TCP,
            client_affinity=self._resolve_client_affinity(ga_config),
        )

        # Create endpoint groups for each configured region
        for region in self.config.get_regions():
            self._create_endpoint_group(region)

        # Export Global Accelerator outputs for other stacks
        self._create_outputs()

        # Apply cdk-nag suppressions
        self._apply_nag_suppressions()

    @staticmethod
    def _resolve_client_affinity(ga_config: dict[str, Any]) -> ga.ClientAffinity:
        """Map the ``client_affinity`` cdk.json knob to a CDK enum.

        Global Accelerator supports two client-affinity modes:

        - ``NONE`` (default): each new connection may be routed to any
          healthy endpoint, maximising even load distribution.
        - ``SOURCE_IP``: connections from the same source IP are pinned to
          the same endpoint, which is useful for workloads that keep
          per-client state on a single region.

        The value is validated up front by
        ``ConfigLoader._validate_global_accelerator_config`` so an unknown
        string never reaches this point; the fallback to ``NONE`` keeps the
        stack synthesizable even when the key is omitted entirely.
        """
        affinity = str(ga_config.get("client_affinity", "NONE")).upper()
        mapping = {
            "NONE": ga.ClientAffinity.NONE,
            "SOURCE_IP": ga.ClientAffinity.SOURCE_IP,
        }
        return mapping.get(affinity, ga.ClientAffinity.NONE)

    def _create_capacity_poller(self) -> None:
        """Create the optional Historical Capacity Surface add-on.

        This is an optional add-on to the global stack (not a separate stack),
        gated by ``historical.enabled`` in cdk.json. It provisions a DynamoDB
        time-series table plus an EventBridge-scheduled Lambda that snapshots
        capacity signals (spot score, spot price, AZ coverage, capacity-block
        availability) for the watched instance types across the enabled regions,
        reusing this stack's DynamoDB/encryption conventions.
        """
        from aws_cdk import aws_events_targets as events_targets
        from aws_cdk import aws_sqs as sqs

        from gco.stacks.nag_suppressions import acknowledge_nag_findings

        project_name = self.config.get_project_name()
        historical = self.config.get_capacity_history_config()
        retention_days = int(historical["retention_days"])
        poll_interval_minutes = int(historical["poll_interval_minutes"])
        watch_instance_types = list(historical["watch_instance_types"])
        enabled_regions = list(historical["enabled_regions"]) or self.config.get_regions()
        # Capacity Block probe durations. ``get_capacity_history_config`` always
        # supplies both keys (defaults merged in), so index directly like the
        # sibling fields above.
        block_duration_hours = int(historical["capacity_block_duration_hours"])
        long_block_duration_hours = int(historical["capacity_block_long_duration_hours"])

        self.capacity_history_table = dynamodb.Table(
            self,
            "CapacityHistoryTable",
            table_name=f"{project_name}-capacity-history",
            partition_key=dynamodb.Attribute(name="pk", type=dynamodb.AttributeType.STRING),
            sort_key=dynamodb.Attribute(name="sk", type=dynamodb.AttributeType.STRING),
            billing_mode=dynamodb.BillingMode.PAY_PER_REQUEST,
            removal_policy=RemovalPolicy.DESTROY,
            point_in_time_recovery_specification=dynamodb.PointInTimeRecoverySpecification(
                point_in_time_recovery_enabled=True
            ),
            encryption=dynamodb.TableEncryption.AWS_MANAGED,
            time_to_live_attribute="ttl",
        )
        self.capacity_history_table.add_global_secondary_index(
            index_name="by-timestamp",
            partition_key=dynamodb.Attribute(
                name="instance_type", type=dynamodb.AttributeType.STRING
            ),
            sort_key=dynamodb.Attribute(name="sk", type=dynamodb.AttributeType.STRING),
            projection_type=dynamodb.ProjectionType.ALL,
        )

        poller_role = iam.Role(
            self,
            "CapacityPollerRole",
            assumed_by=iam.ServicePrincipal("lambda.amazonaws.com"),
            managed_policies=[
                iam.ManagedPolicy.from_aws_managed_policy_name(
                    "service-role/AWSLambdaBasicExecutionRole"
                )
            ],
        )
        poller_role.add_to_policy(
            iam.PolicyStatement(
                effect=iam.Effect.ALLOW,
                actions=["dynamodb:PutItem", "dynamodb:BatchWriteItem"],
                resources=[
                    self.capacity_history_table.table_arn,
                    f"{self.capacity_history_table.table_arn}/index/*",
                ],
            )
        )
        poller_role.add_to_policy(
            iam.PolicyStatement(
                effect=iam.Effect.ALLOW,
                actions=[
                    "ec2:DescribeSpotPriceHistory",
                    "ec2:GetSpotPlacementScores",
                    "ec2:DescribeCapacityBlockOfferings",
                    "ec2:DescribeCapacityReservations",
                    "ec2:DescribeAvailabilityZones",
                ],
                resources=["*"],
            )
        )

        self.capacity_poller_lambda = lambda_.Function(
            self,
            "CapacityPollerFunction",
            runtime=getattr(lambda_.Runtime, LAMBDA_PYTHON_RUNTIME),
            handler="handler.lambda_handler",
            code=lambda_.Code.from_asset("lambda/capacity-poller"),
            timeout=Duration.minutes(14),
            memory_size=256,
            role=poller_role,
            environment={
                "CAPACITY_HISTORY_TABLE_NAME": self.capacity_history_table.table_name,
                "WATCH_INSTANCE_TYPES": ",".join(watch_instance_types),
                "ENABLED_REGIONS": ",".join(enabled_regions),
                "CAPACITY_HISTORY_RETENTION_DAYS": str(retention_days),
                "CAPACITY_BLOCK_DURATION_HOURS": str(block_duration_hours),
                "CAPACITY_BLOCK_LONG_DURATION_HOURS": str(long_block_duration_hours),
            },
            tracing=lambda_.Tracing.ACTIVE,
            description=(
                "Historical Capacity Surface poller (optional global-stack add-on): "
                "snapshots capacity signals into the capacity-history table."
            ),
        )

        poller_dlq = sqs.Queue(
            self,
            "CapacityPollerRuleDlq",
            retention_period=Duration.days(14),
            enforce_ssl=True,
            encryption=sqs.QueueEncryption.SQS_MANAGED,
            removal_policy=RemovalPolicy.DESTROY,
        )
        events.Rule(
            self,
            "CapacityPollerSchedule",
            description=(
                f"Capacity poller for {project_name} history surface "
                f"(every {poll_interval_minutes} min)"
            ),
            schedule=events.Schedule.rate(Duration.minutes(poll_interval_minutes)),
            targets=[
                events_targets.LambdaFunction(
                    self.capacity_poller_lambda, dead_letter_queue=poller_dlq, retry_attempts=2
                )
            ],
        )

        ssm.StringParameter(
            self,
            "CapacityHistoryTableNameParam",
            parameter_name=f"/{project_name}/capacity-history-table-name",
            string_value=self.capacity_history_table.table_name,
            description="DynamoDB table name for historical capacity snapshots",
        )
        CfnOutput(
            self,
            "CapacityHistoryTableName",
            value=self.capacity_history_table.table_name,
            description="DynamoDB table name for historical capacity snapshots",
            export_name=f"{project_name}-capacity-history-table-name",
        )
        CfnOutput(
            self,
            "CapacityHistoryTableArn",
            value=self.capacity_history_table.table_arn,
            description="DynamoDB table ARN for historical capacity snapshots",
            export_name=f"{project_name}-capacity-history-table-arn",
        )

        acknowledge_nag_findings(
            poller_role,
            [
                {
                    "id": "AwsSolutions-IAM4",
                    "reason": (
                        "AWSLambdaBasicExecutionRole provides the standard CloudWatch "
                        "Logs permissions every Lambda needs."
                    ),
                },
                {
                    "id": "AwsSolutions-IAM5",
                    "reason": (
                        "The EC2 capacity describe/get APIs (DescribeSpotPriceHistory, "
                        "GetSpotPlacementScores, DescribeCapacityBlockOfferings, "
                        "DescribeCapacityReservations, DescribeAvailabilityZones) do not "
                        "support resource-level permissions and require a wildcard "
                        "resource. The DynamoDB index wildcard is scoped to this table's "
                        "own indexes."
                    ),
                    "appliesTo": [
                        "Resource::*",
                        "Resource::<CapacityHistoryTable506A0FBA.Arn>/index/*",
                    ],
                },
            ],
        )
        acknowledge_nag_findings(
            poller_dlq,
            [
                {
                    "id": "AwsSolutions-SQS3",
                    "reason": (
                        "This queue is the dead-letter queue for the "
                        "CapacityPollerSchedule EventBridge rule; a DLQ for a DLQ is "
                        "circular."
                    ),
                },
                {
                    "id": "Serverless-SQSRedrivePolicy",
                    "reason": (
                        "This queue is itself the dead-letter queue for the "
                        "CapacityPollerSchedule EventBridge rule, so it does not need "
                        "its own redrive policy; a DLQ for a DLQ is circular."
                    ),
                },
            ],
        )
        acknowledge_nag_findings(
            self.capacity_history_table,
            [
                {
                    "id": "HIPAA.Security-DynamoDBInBackupPlan",
                    "reason": (
                        "The capacity-history table holds ephemeral, reconstructable "
                        "telemetry snapshots with a TTL (default 90 days) and "
                        "point-in-time recovery enabled. The poller re-collects this "
                        "data continuously, so an AWS Backup plan is unnecessary for "
                        "this optional add-on."
                    ),
                },
                {
                    "id": "NIST.800.53.R5-DynamoDBInBackupPlan",
                    "reason": (
                        "The capacity-history table holds ephemeral, reconstructable "
                        "telemetry snapshots with a TTL (default 90 days) and "
                        "point-in-time recovery enabled. The poller re-collects this "
                        "data continuously, so an AWS Backup plan is unnecessary for "
                        "this optional add-on."
                    ),
                },
            ],
        )

    def _create_outputs(self) -> None:
        """Create CloudFormation outputs for cross-stack references."""
        project_name = self.config.get_project_name()

        CfnOutput(
            self,
            "GlobalAcceleratorDnsName",
            value=self.accelerator.dns_name,
            description="Global Accelerator DNS name for global endpoint",
            export_name=f"{project_name}-global-accelerator-dns",
        )

        CfnOutput(
            self,
            "GlobalAcceleratorArn",
            value=self.accelerator.accelerator_arn,
            description="Global Accelerator ARN",
            export_name=f"{project_name}-global-accelerator-arn",
        )

        CfnOutput(
            self,
            "GlobalAcceleratorListenerArn",
            value=self.listener.listener_arn,
            description="Global Accelerator Listener ARN",
            export_name=f"{project_name}-global-accelerator-listener-arn",
        )

    def _apply_nag_suppressions(self) -> None:
        """Apply cdk-nag suppressions for this stack."""
        from gco.stacks.nag_suppressions import apply_all_suppressions

        apply_all_suppressions(
            self, stack_type="global", project_name=self.config.get_project_name()
        )

    def _create_endpoint_group(self, region: str) -> None:
        """
        Create an endpoint group for a specific region.

        Configures HTTP health checks using the path from cdk.json so
        Global Accelerator can verify the ALB's backend services are
        actually healthy (not just that the port is open).

        Also stores the endpoint group ARN in SSM Parameter Store for
        cross-region access by regional stacks.

        Args:
            region: AWS region name (e.g., 'us-east-1')
        """
        project_name = self.config.get_project_name()
        region_id = region.replace("-", "").title()
        ga_config = self.config.get_global_accelerator_config()

        # Use HTTP health checks so GA validates the backend services are
        # actually responding, not just that the ALB port is open.
        # The health_check_path from cdk.json (default: /api/v1/health)
        # hits the health-monitor service behind the ALB.
        endpoint_group = self.listener.add_endpoint_group(
            f"EndpointGroup{region_id}",
            region=region,
            health_check_port=80,
            health_check_protocol=ga.HealthCheckProtocol.HTTP,
            health_check_path=ga_config.get("health_check_path", "/api/v1/health"),
            health_check_interval=Duration.seconds(ga_config.get("health_check_interval", 30)),
            health_check_threshold=3,
        )

        self.endpoint_groups[region] = endpoint_group

        # Export endpoint group ARN for regional stacks
        CfnOutput(
            self,
            f"EndpointGroup{region_id}Arn",
            value=endpoint_group.endpoint_group_arn,
            description=f"Endpoint group ARN for {region}",
            export_name=f"{project_name}-endpoint-group-{region}-arn",
        )

        # Store endpoint group ARN in SSM Parameter Store for cross-region access
        # Regional stacks read this to register their ALBs with Global Accelerator
        ssm.StringParameter(
            self,
            f"EndpointGroup{region_id}ArnParam",
            parameter_name=f"/{project_name}/endpoint-group-{region}-arn",
            string_value=endpoint_group.endpoint_group_arn,
            description=f"Global Accelerator endpoint group ARN for {region}",
        )

    def add_regional_endpoint(self, region: str, alb_arn: str) -> None:
        """Add a regional ALB endpoint to the Global Accelerator.

        Note: Due to cross-region reference limitations in CDK, the actual endpoint
        registration is handled by a custom resource in the regional stack.
        This method stores the ARN for reference but doesn't directly register it.

        The regional stack should use the endpoint group ARN exported by this stack
        to register its ALB via an AwsCustomResource.
        """
        self.regional_endpoints[region] = alb_arn
        # Actual registration happens in regional stack via custom resource

    def get_accelerator_dns_name(self) -> str:
        """Get the Global Accelerator DNS name"""
        return str(self.accelerator.dns_name)

    def get_accelerator_arn(self) -> str:
        """Get the Global Accelerator ARN"""
        return str(self.accelerator.accelerator_arn)

    def get_listener_arn(self) -> str:
        """Get the Global Accelerator Listener ARN"""
        return str(self.listener.listener_arn)

    def get_endpoint_group_arn(self, region: str) -> str:
        """Get the endpoint group ARN for a specific region"""
        if region in self.endpoint_groups:
            return str(self.endpoint_groups[region].endpoint_group_arn)
        raise ValueError(f"No endpoint group found for region: {region}")

    def _create_dynamodb_tables(self) -> None:
        """Create DynamoDB tables for templates, webhooks, jobs, inference endpoints, and missions."""
        project_name = self.config.get_project_name()

        # Job Templates table - stores reusable job templates
        self.templates_table = dynamodb.Table(
            self,
            "JobTemplatesTable",
            table_name=f"{project_name}-job-templates",
            partition_key=dynamodb.Attribute(
                name="template_name",
                type=dynamodb.AttributeType.STRING,
            ),
            billing_mode=dynamodb.BillingMode.PAY_PER_REQUEST,
            removal_policy=RemovalPolicy.DESTROY,
            point_in_time_recovery_specification=dynamodb.PointInTimeRecoverySpecification(
                point_in_time_recovery_enabled=True
            ),
            encryption=dynamodb.TableEncryption.AWS_MANAGED,
        )

        # Webhooks table - stores webhook registrations
        self.webhooks_table = dynamodb.Table(
            self,
            "WebhooksTable",
            table_name=f"{project_name}-webhooks",
            partition_key=dynamodb.Attribute(
                name="webhook_id",
                type=dynamodb.AttributeType.STRING,
            ),
            billing_mode=dynamodb.BillingMode.PAY_PER_REQUEST,
            removal_policy=RemovalPolicy.DESTROY,
            point_in_time_recovery_specification=dynamodb.PointInTimeRecoverySpecification(
                point_in_time_recovery_enabled=True
            ),
            encryption=dynamodb.TableEncryption.AWS_MANAGED,
        )

        # Add GSI for querying webhooks by namespace
        self.webhooks_table.add_global_secondary_index(
            index_name="namespace-index",
            partition_key=dynamodb.Attribute(
                name="namespace",
                type=dynamodb.AttributeType.STRING,
            ),
            projection_type=dynamodb.ProjectionType.ALL,
        )

        # Jobs table - centralized job tracking and queue
        # This enables global job submission with regional pickup
        self.jobs_table = dynamodb.Table(
            self,
            "JobsTable",
            table_name=f"{project_name}-jobs",
            partition_key=dynamodb.Attribute(
                name="job_id",
                type=dynamodb.AttributeType.STRING,
            ),
            billing_mode=dynamodb.BillingMode.PAY_PER_REQUEST,
            removal_policy=RemovalPolicy.DESTROY,
            point_in_time_recovery_specification=dynamodb.PointInTimeRecoverySpecification(
                point_in_time_recovery_enabled=True
            ),
            encryption=dynamodb.TableEncryption.AWS_MANAGED,
            time_to_live_attribute="ttl",  # Auto-cleanup old completed jobs
        )

        # GSI for querying jobs by region and status (for regional polling)
        self.jobs_table.add_global_secondary_index(
            index_name="region-status-index",
            partition_key=dynamodb.Attribute(
                name="target_region",
                type=dynamodb.AttributeType.STRING,
            ),
            sort_key=dynamodb.Attribute(
                name="status",
                type=dynamodb.AttributeType.STRING,
            ),
            projection_type=dynamodb.ProjectionType.ALL,
        )

        # GSI for querying jobs by namespace
        self.jobs_table.add_global_secondary_index(
            index_name="namespace-index",
            partition_key=dynamodb.Attribute(
                name="namespace",
                type=dynamodb.AttributeType.STRING,
            ),
            sort_key=dynamodb.Attribute(
                name="submitted_at",
                type=dynamodb.AttributeType.STRING,
            ),
            projection_type=dynamodb.ProjectionType.ALL,
        )

        # GSI for querying jobs by status globally
        self.jobs_table.add_global_secondary_index(
            index_name="status-index",
            partition_key=dynamodb.Attribute(
                name="status",
                type=dynamodb.AttributeType.STRING,
            ),
            sort_key=dynamodb.Attribute(
                name="submitted_at",
                type=dynamodb.AttributeType.STRING,
            ),
            projection_type=dynamodb.ProjectionType.ALL,
        )

        # Export table names and ARNs for regional stacks
        CfnOutput(
            self,
            "TemplatesTableName",
            value=self.templates_table.table_name,
            description="DynamoDB table name for job templates",
            export_name=f"{project_name}-templates-table-name",
        )

        CfnOutput(
            self,
            "TemplatesTableArn",
            value=self.templates_table.table_arn,
            description="DynamoDB table ARN for job templates",
            export_name=f"{project_name}-templates-table-arn",
        )

        CfnOutput(
            self,
            "WebhooksTableName",
            value=self.webhooks_table.table_name,
            description="DynamoDB table name for webhooks",
            export_name=f"{project_name}-webhooks-table-name",
        )

        CfnOutput(
            self,
            "WebhooksTableArn",
            value=self.webhooks_table.table_arn,
            description="DynamoDB table ARN for webhooks",
            export_name=f"{project_name}-webhooks-table-arn",
        )

        CfnOutput(
            self,
            "JobsTableName",
            value=self.jobs_table.table_name,
            description="DynamoDB table name for centralized job tracking",
            export_name=f"{project_name}-jobs-table-name",
        )

        CfnOutput(
            self,
            "JobsTableArn",
            value=self.jobs_table.table_arn,
            description="DynamoDB table ARN for centralized job tracking",
            export_name=f"{project_name}-jobs-table-arn",
        )

        # Inference Endpoints table - stores desired state for inference deployments
        # The inference_monitor in each regional cluster polls this table
        self.inference_endpoints_table = dynamodb.Table(
            self,
            "InferenceEndpointsTable",
            table_name=f"{project_name}-inference-endpoints",
            partition_key=dynamodb.Attribute(
                name="endpoint_name",
                type=dynamodb.AttributeType.STRING,
            ),
            billing_mode=dynamodb.BillingMode.PAY_PER_REQUEST,
            removal_policy=RemovalPolicy.DESTROY,
            point_in_time_recovery_specification=dynamodb.PointInTimeRecoverySpecification(
                point_in_time_recovery_enabled=True
            ),
            encryption=dynamodb.TableEncryption.AWS_MANAGED,
        )

        CfnOutput(
            self,
            "InferenceEndpointsTableName",
            value=self.inference_endpoints_table.table_name,
            description="DynamoDB table name for inference endpoint state",
            export_name=f"{project_name}-inference-endpoints-table-name",
        )

        CfnOutput(
            self,
            "InferenceEndpointsTableArn",
            value=self.inference_endpoints_table.table_arn,
            description="DynamoDB table ARN for inference endpoint state",
            export_name=f"{project_name}-inference-endpoints-table-arn",
        )

        # Missions table - persists goal-directed iteration session state
        # Partition by session_id; the status-index GSI supports paginated
        # listing by status (e.g. running, completed, terminated, failed).
        self.missions_table = dynamodb.Table(
            self,
            "MissionsTable",
            table_name=f"{project_name}-missions",
            partition_key=dynamodb.Attribute(
                name="session_id",
                type=dynamodb.AttributeType.STRING,
            ),
            billing_mode=dynamodb.BillingMode.PAY_PER_REQUEST,
            removal_policy=RemovalPolicy.DESTROY,
            point_in_time_recovery_specification=dynamodb.PointInTimeRecoverySpecification(
                point_in_time_recovery_enabled=True
            ),
            encryption=dynamodb.TableEncryption.AWS_MANAGED,
        )

        # GSI for paginating sessions by status (sorted by creation time)
        self.missions_table.add_global_secondary_index(
            index_name="status-index",
            partition_key=dynamodb.Attribute(
                name="status",
                type=dynamodb.AttributeType.STRING,
            ),
            sort_key=dynamodb.Attribute(
                name="created_at",
                type=dynamodb.AttributeType.STRING,
            ),
            projection_type=dynamodb.ProjectionType.ALL,
        )

        CfnOutput(
            self,
            "MissionsTableName",
            value=self.missions_table.table_name,
            description="DynamoDB table name for mission session state",
            export_name=f"{project_name}-missions-table-name",
        )

        CfnOutput(
            self,
            "MissionsTableArn",
            value=self.missions_table.table_arn,
            description="DynamoDB table ARN for mission session state",
            export_name=f"{project_name}-missions-table-arn",
        )

        # Store table names in SSM for cross-region access
        ssm.StringParameter(
            self,
            "TemplatesTableNameParam",
            parameter_name=f"/{project_name}/templates-table-name",
            string_value=self.templates_table.table_name,
            description="DynamoDB table name for job templates",
        )

        ssm.StringParameter(
            self,
            "WebhooksTableNameParam",
            parameter_name=f"/{project_name}/webhooks-table-name",
            string_value=self.webhooks_table.table_name,
            description="DynamoDB table name for webhooks",
        )

        ssm.StringParameter(
            self,
            "JobsTableNameParam",
            parameter_name=f"/{project_name}/jobs-table-name",
            string_value=self.jobs_table.table_name,
            description="DynamoDB table name for centralized job tracking",
        )

        ssm.StringParameter(
            self,
            "InferenceEndpointsTableNameParam",
            parameter_name=f"/{project_name}/inference-endpoints-table-name",
            string_value=self.inference_endpoints_table.table_name,
            description="DynamoDB table name for inference endpoint state",
        )

        ssm.StringParameter(
            self,
            "MissionsTableNameParam",
            parameter_name=f"/{project_name}/missions-table-name",
            string_value=self.missions_table.table_name,
            description="DynamoDB table name for mission session state",
        )

    def _create_model_bucket(self) -> None:
        """Create S3 bucket for model weights.

        This bucket serves as the central model registry. Users upload model
        weights here once, and the inference_monitor's init containers sync
        them to each region's local EFS at pod startup.

        The bucket name is auto-generated by CDK to avoid naming collisions.
        It's exported via CfnOutput and SSM for CLI discovery.
        """
        project_name = self.config.get_project_name()

        # KMS key for model bucket encryption
        self.model_bucket_key = kms.Key(
            self,
            "ModelBucketKey",
            description="KMS key for GCO model weights bucket",
            enable_key_rotation=True,
            removal_policy=RemovalPolicy.DESTROY,
        )

        # Access logs bucket (required for compliance)
        # Retention is configurable via cdk.json context field `s3_access_logs.retention_days`
        # (default: 90 days). Logs older than the configured retention are expired.
        s3_access_logs_ctx = self.node.try_get_context("s3_access_logs") or {}
        access_logs_retention_days = int(s3_access_logs_ctx.get("retention_days", 90))

        self.model_bucket_access_logs = s3.Bucket(
            self,
            "ModelWeightsAccessLogsBucket",
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

        # Model weights bucket
        self.model_bucket = s3.Bucket(
            self,
            "ModelWeightsBucket",
            encryption=s3.BucketEncryption.KMS,
            encryption_key=self.model_bucket_key,
            bucket_key_enabled=True,
            block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
            enforce_ssl=True,
            versioned=True,
            removal_policy=RemovalPolicy.DESTROY,
            auto_delete_objects=True,
            server_access_logs_bucket=self.model_bucket_access_logs,
            server_access_logs_prefix="model-bucket-logs/",
        )

        # CDK-nag suppressions — only replication (not needed for model weights)
        from gco.stacks.nag_suppressions import acknowledge_nag_findings

        replication_reason = (
            "Model weights are user-uploaded artifacts that can be re-uploaded. "
            "Cross-region replication is not required; the inference_monitor "
            "syncs models from S3 to each region's EFS at pod startup."
        )

        acknowledge_nag_findings(
            self.model_bucket,
            [
                {
                    "id": "HIPAA.Security-S3BucketReplicationEnabled",
                    "reason": replication_reason,
                },
                {
                    "id": "NIST.800.53.R5-S3BucketReplicationEnabled",
                    "reason": replication_reason,
                },
                {
                    "id": "PCI.DSS.321-S3BucketReplicationEnabled",
                    "reason": replication_reason,
                },
            ],
        )

        logs_reason = "This is the server access logs destination bucket."
        acknowledge_nag_findings(
            self.model_bucket_access_logs,
            [
                {"id": "AwsSolutions-S1", "reason": logs_reason},
                {"id": "HIPAA.Security-S3BucketLoggingEnabled", "reason": logs_reason},
                {
                    "id": "HIPAA.Security-S3BucketReplicationEnabled",
                    "reason": "Access logs do not require replication.",
                },
                {
                    "id": "HIPAA.Security-S3DefaultEncryptionKMS",
                    "reason": "SSE-S3 is sufficient for access logs.",
                },
                {"id": "NIST.800.53.R5-S3BucketLoggingEnabled", "reason": logs_reason},
                {
                    "id": "NIST.800.53.R5-S3BucketReplicationEnabled",
                    "reason": "Access logs do not require replication.",
                },
                {
                    "id": "NIST.800.53.R5-S3DefaultEncryptionKMS",
                    "reason": "SSE-S3 is sufficient for access logs.",
                },
                {"id": "PCI.DSS.321-S3BucketLoggingEnabled", "reason": logs_reason},
                {
                    "id": "PCI.DSS.321-S3BucketReplicationEnabled",
                    "reason": "Access logs do not require replication.",
                },
                {
                    "id": "PCI.DSS.321-S3DefaultEncryptionKMS",
                    "reason": "SSE-S3 is sufficient for access logs.",
                },
            ],
        )

        CfnOutput(
            self,
            "ModelBucketName",
            value=self.model_bucket.bucket_name,
            description="S3 bucket for model weights",
            export_name=f"{project_name}-model-bucket-name",
        )

        CfnOutput(
            self,
            "ModelBucketArn",
            value=self.model_bucket.bucket_arn,
            description="S3 bucket ARN for model weights",
            export_name=f"{project_name}-model-bucket-arn",
        )

        ssm.StringParameter(
            self,
            "ModelBucketNameParam",
            parameter_name=f"/{project_name}/model-bucket-name",
            string_value=self.model_bucket.bucket_name,
            description="S3 bucket name for model weights",
        )

    def _create_backup_plan(self) -> None:
        """Create AWS Backup plan for DynamoDB tables.

        Creates a backup plan with:
        - Daily backups retained for 35 days
        - Weekly backups retained for 90 days
        - All DynamoDB tables added to the backup selection
        """
        # Create backup vault for storing backups
        self.backup_vault = backup.BackupVault(
            self,
            "DynamoDBBackupVault",
            removal_policy=RemovalPolicy.DESTROY,
        )

        # Create backup plan with daily and weekly rules
        self.backup_plan = backup.BackupPlan(
            self,
            "DynamoDBBackupPlan",
            backup_plan_rules=[
                # Daily backup - retained for 35 days
                backup.BackupPlanRule(
                    rule_name="DailyBackup",
                    backup_vault=self.backup_vault,
                    schedule_expression=events.Schedule.cron(
                        hour="3",
                        minute="0",
                    ),
                    delete_after=Duration.days(35),
                    enable_continuous_backup=True,  # Enable PITR for DynamoDB
                ),
                # Weekly backup - retained for 90 days
                backup.BackupPlanRule(
                    rule_name="WeeklyBackup",
                    backup_vault=self.backup_vault,
                    schedule_expression=events.Schedule.cron(
                        hour="4",
                        minute="0",
                        week_day="SUN",
                    ),
                    delete_after=Duration.days(90),
                ),
            ],
        )

        # Add all DynamoDB tables to the backup selection
        self.backup_plan.add_selection(
            "DynamoDBTablesSelection",
            resources=[
                backup.BackupResource.from_dynamo_db_table(self.templates_table),
                backup.BackupResource.from_dynamo_db_table(self.webhooks_table),
                backup.BackupResource.from_dynamo_db_table(self.jobs_table),
                backup.BackupResource.from_dynamo_db_table(self.inference_endpoints_table),
                backup.BackupResource.from_dynamo_db_table(self.missions_table),
            ],
        )

        # Export backup plan ARN
        project_name = self.config.get_project_name()
        CfnOutput(
            self,
            "BackupPlanArn",
            value=self.backup_plan.backup_plan_arn,
            description="AWS Backup plan ARN for DynamoDB tables",
            export_name=f"{project_name}-backup-plan-arn",
        )

        CfnOutput(
            self,
            "BackupVaultArn",
            value=self.backup_vault.backup_vault_arn,
            description="AWS Backup vault ARN for DynamoDB backups",
            export_name=f"{project_name}-backup-vault-arn",
        )

    def _create_cluster_shared_kms_key(self) -> None:
        """Create the always-on customer-managed KMS key for ``Cluster_Shared_Bucket``.

        The key:
        - Enables automatic annual rotation.
        - Uses a 7-day pending window on destroy — the AWS minimum, matching the
          destroy-by-default iteration-loop posture of the analytics-environment
          feature while still providing a safety net against accidental deletion.
        - Uses ``RemovalPolicy.DESTROY`` so a ``cdk destroy gco-global`` cleans up
          the key without operator intervention (iteration-loop posture).
        - Grants encrypt/decrypt to the ``s3.amazonaws.com`` and
          ``logs.<region>.amazonaws.com`` service principals via the key policy
          so S3 server-side encryption and CloudWatch access-log delivery can use
          the key without role-side grants.

        The key is exposed as ``self.cluster_shared_kms_key`` for tests and for
        ``_create_cluster_shared_bucket`` to reference. Role-side usage grants
        (``kms:Decrypt`` / ``kms:GenerateDataKey``) are attached by downstream
        consumers: ``GCORegionalStack`` on the job-pod role (always-on)
        and ``GCOAnalyticsStack`` on the SageMaker execution role (conditional on
        the analytics toggle).
        """
        self.cluster_shared_kms_key = kms.Key(
            self,
            "ClusterSharedKmsKey",
            description=(
                "Customer-managed KMS key for the always-on Cluster_Shared_Bucket "
                "in GCOGlobalStack. Consumed by every regional EKS cluster and by "
                "GCOAnalyticsStack when analytics is enabled."
            ),
            enable_key_rotation=True,
            pending_window=Duration.days(7),
            removal_policy=RemovalPolicy.DESTROY,
        )

        # Key-policy grants for service principals that need to encrypt/decrypt
        # on behalf of the bucket (S3 server-side encryption) and the access-logs
        # bucket (CloudWatch Logs delivery). The actions match the standard
        # service-principal pattern used by cdk's default key policies.
        kms_actions = [
            "kms:Encrypt",
            "kms:Decrypt",
            "kms:ReEncrypt*",
            "kms:GenerateDataKey*",
            "kms:DescribeKey",
        ]

        self.cluster_shared_kms_key.add_to_resource_policy(
            iam.PolicyStatement(
                sid="AllowS3ServiceEncryptDecrypt",
                effect=iam.Effect.ALLOW,
                principals=[iam.ServicePrincipal("s3.amazonaws.com")],
                actions=kms_actions,
                resources=["*"],
            )
        )

        self.cluster_shared_kms_key.add_to_resource_policy(
            iam.PolicyStatement(
                sid="AllowCloudWatchLogsEncryptDecrypt",
                effect=iam.Effect.ALLOW,
                principals=[iam.ServicePrincipal(f"logs.{self.region}.amazonaws.com")],
                actions=kms_actions,
                resources=["*"],
            )
        )

    def _create_cluster_shared_bucket(self) -> None:
        """Create the always-on ``Cluster_Shared_Bucket`` and its access-logs bucket.

        Two buckets are created:

        1. ``cluster_shared_access_logs_bucket`` — dedicated S3 access-logs bucket
           used as ``server_access_logs_bucket`` for the primary bucket. Separate
           from ``model_bucket_access_logs`` so cluster-shared-bucket access logs
           are not commingled with model-bucket logs.
        2. ``cluster_shared_bucket`` — the primary bucket named
           ``<project_name>-cluster-shared-<account>-<global-region>`` (the
           prefix from ``cluster_shared_bucket_name_prefix(project_name)`` is
           the stable ARN prefix used by IAM policies and nag assertions).
           KMS-encrypted with
           ``cluster_shared_kms_key``, block-public-access on, SSL enforced,
           versioned, destroy-on-teardown.

        An explicit ``Deny`` statement for ``aws:SecureTransport=false`` is added
        to the bucket policy independent of ``enforce_ssl=True`` so the deny is
        verifiable in the synthesized template (belt-and-suspenders).

        Grants on ``Cluster_Shared_Bucket`` are intentionally not added here —
        they live on downstream role policies (``GCORegionalStack`` on the
        job-pod role, ``GCOAnalyticsStack`` on the SageMaker execution role)
        rather than in this bucket's policy. The bucket policy contains zero
        ``Principal: "*"`` Allow statements.
        """
        # Retention for the access-logs bucket honors the same `s3_access_logs`
        # context field as the model-bucket access-logs bucket (default 90 days).
        s3_access_logs_ctx = self.node.try_get_context("s3_access_logs") or {}
        access_logs_retention_days = int(s3_access_logs_ctx.get("retention_days", 90))

        # Dedicated access-logs bucket for Cluster_Shared_Bucket. Encrypted with
        # the cluster-shared KMS key (the key policy grants the logs service
        # principal encrypt/decrypt). Kept separate from model_bucket_access_logs
        # so operators can reason about each bucket's logs independently. Matches
        # the LifecycleRule used on `model_bucket_access_logs` so retention is
        # consistent across the two log sinks.
        self.cluster_shared_access_logs_bucket = s3.Bucket(
            self,
            "ClusterSharedAccessLogsBucket",
            encryption=s3.BucketEncryption.KMS,
            encryption_key=self.cluster_shared_kms_key,
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

        # Primary Cluster_Shared_Bucket. Name is derived from ``project_name``
        # so the bucket and the IAM allow-list assertion
        # (arn:aws:s3:::<project_name>-cluster-shared-*) stay in lockstep and
        # two deployments in the same account+region do not collide.
        # `bucket_key_enabled=True` mirrors the model_bucket pattern to reduce
        # per-object KMS request costs.
        project_name = self.config.get_project_name()
        self.cluster_shared_bucket = s3.Bucket(
            self,
            "ClusterSharedBucket",
            bucket_name=f"{cluster_shared_bucket_name_prefix(project_name)}-{self.account}-{self.region}",
            encryption=s3.BucketEncryption.KMS,
            encryption_key=self.cluster_shared_kms_key,
            bucket_key_enabled=True,
            block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
            enforce_ssl=True,
            versioned=True,
            removal_policy=RemovalPolicy.DESTROY,
            auto_delete_objects=True,
            server_access_logs_bucket=self.cluster_shared_access_logs_bucket,
            server_access_logs_prefix="cluster-shared/",
        )

        # Explicit Deny for insecure transport. `enforce_ssl=True` already adds
        # an equivalent statement, but duplicating it here makes the deny
        # verifiable in the synthesized template under a known SID and satisfies
        # a belt-and-suspenders posture.
        self.cluster_shared_bucket.add_to_resource_policy(
            iam.PolicyStatement(
                sid="DenyInsecureTransport",
                effect=iam.Effect.DENY,
                principals=[iam.AnyPrincipal()],
                actions=["s3:*"],
                resources=[
                    self.cluster_shared_bucket.bucket_arn,
                    f"{self.cluster_shared_bucket.bucket_arn}/*",
                ],
                conditions={"Bool": {"aws:SecureTransport": "false"}},
            )
        )

        # CDK-nag suppressions — scoped per-resource at the construct site to
        # mirror the ``_create_model_bucket`` pattern (keeps the suppression
        # co-located with the construct it applies to, so the reason survives
        # refactors). Every suppression carries an explicit reason
        # string; no blanket ``Resource::*`` bypasses.
        from gco.stacks.nag_suppressions import acknowledge_nag_findings

        shared_replication_reason = (
            "Cluster_Shared_Bucket is a regional scratch sink; cluster jobs "
            "publish to it from a single region, and there is no durability "
            "requirement that warrants cross-region replication. Access logs "
            "do not require replication for the same reason."
        )

        acknowledge_nag_findings(
            self.cluster_shared_bucket,
            [
                {
                    "id": "HIPAA.Security-S3BucketReplicationEnabled",
                    "reason": shared_replication_reason,
                },
                {
                    "id": "NIST.800.53.R5-S3BucketReplicationEnabled",
                    "reason": shared_replication_reason,
                },
                {
                    "id": "PCI.DSS.321-S3BucketReplicationEnabled",
                    "reason": shared_replication_reason,
                },
            ],
        )

        access_logs_is_self_target_reason = (
            "This is the server access logs destination bucket for Cluster_Shared_Bucket."
        )
        acknowledge_nag_findings(
            self.cluster_shared_access_logs_bucket,
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
                    "reason": shared_replication_reason,
                },
                {
                    "id": "NIST.800.53.R5-S3BucketReplicationEnabled",
                    "reason": shared_replication_reason,
                },
                {
                    "id": "PCI.DSS.321-S3BucketReplicationEnabled",
                    "reason": shared_replication_reason,
                },
            ],
        )

    def _publish_cluster_shared_bucket_ssm_params(self) -> None:
        """Publish the three ``/gco/cluster-shared-bucket/*`` SSM parameters.

        Writes:

        - ``/gco/cluster-shared-bucket/name`` — bucket name
        - ``/gco/cluster-shared-bucket/arn`` — bucket ARN
        - ``/gco/cluster-shared-bucket/region`` — bucket home region (global region)

        These parameters are the cross-region contract consumed by
        ``GCORegionalStack._resolve_cluster_shared_bucket_from_ssm`` (always) and by
        ``GCOAnalyticsStack._grant_sagemaker_role_on_cluster_shared_bucket``
        (conditional on the analytics toggle). The prefix from
        ``cluster_shared_ssm_parameter_prefix(project_name)`` is the single
        source of truth so the namespace can be renamed in exactly one place.

        Also emits four ``CfnOutput`` values for discoverability: the three SSM
        values plus the KMS key ARN. Export names follow the existing
        ``{project_name}-cluster-shared-{suffix}`` pattern used by the rest of
        this stack's outputs so operators can cross-reference them from peer
        stacks via ``Fn.import_value`` if needed (the primary cross-region
        contract remains SSM).
        """
        project_name = self.config.get_project_name()

        ssm.StringParameter(
            self,
            "ClusterSharedBucketNameParam",
            parameter_name=f"{cluster_shared_ssm_parameter_prefix(project_name)}/name",
            string_value=self.cluster_shared_bucket.bucket_name,
            description="Name of the always-on Cluster_Shared_Bucket (owned by GCOGlobalStack).",
        )

        ssm.StringParameter(
            self,
            "ClusterSharedBucketArnParam",
            parameter_name=f"{cluster_shared_ssm_parameter_prefix(project_name)}/arn",
            string_value=self.cluster_shared_bucket.bucket_arn,
            description="ARN of the always-on Cluster_Shared_Bucket (owned by GCOGlobalStack).",
        )

        ssm.StringParameter(
            self,
            "ClusterSharedBucketRegionParam",
            parameter_name=f"{cluster_shared_ssm_parameter_prefix(project_name)}/region",
            string_value=self.region,
            description="Home region of the always-on Cluster_Shared_Bucket (the global region).",
        )

        CfnOutput(
            self,
            "ClusterSharedBucketName",
            value=self.cluster_shared_bucket.bucket_name,
            description="Name of the always-on Cluster_Shared_Bucket.",
            export_name=f"{project_name}-cluster-shared-bucket-name",
        )

        CfnOutput(
            self,
            "ClusterSharedBucketArn",
            value=self.cluster_shared_bucket.bucket_arn,
            description="ARN of the always-on Cluster_Shared_Bucket.",
            export_name=f"{project_name}-cluster-shared-bucket-arn",
        )

        CfnOutput(
            self,
            "ClusterSharedBucketRegion",
            value=self.region,
            description="Home region of the always-on Cluster_Shared_Bucket.",
            export_name=f"{project_name}-cluster-shared-bucket-region",
        )

        CfnOutput(
            self,
            "ClusterSharedKmsKeyArn",
            value=self.cluster_shared_kms_key.key_arn,
            description="ARN of the always-on KMS key encrypting Cluster_Shared_Bucket.",
            export_name=f"{project_name}-cluster-shared-kms-key-arn",
        )

    def _resolve_replication_destinations(self, destinations: str | list[str]) -> list[str]:
        """Resolve the configured replication destinations into a region list.

        When ``destinations`` is the literal ``"all_deployed_regions"``, the
        list comes from ``self.config.get_regions()`` (the same source the
        rest of the stack uses for cross-region wiring). When it is an
        explicit list, it is returned as-is. The source region (the global
        stack's deploy region) is excluded — ECR replication is point-to-point
        and a self-referential destination is rejected by the API.
        """
        if isinstance(destinations, str):
            candidate_regions = list(self.config.get_regions())
        else:
            candidate_regions = list(destinations)
        return [region for region in candidate_regions if region != self.region]

    def _create_image_replication_rule(self) -> None:
        """Provision the ECR replication rule for ``gco/*`` repositories.

        When ``images.replication.enabled`` is True and at least one
        non-source destination resolves, creates one
        ``aws_ecr.CfnReplicationConfiguration`` rule with a single
        ``PREFIX_MATCH`` filter on ``gco/`` and one destination per resolved
        region. When replication is disabled or the destination list is
        empty (e.g. single-region deploy), no replication resource is
        provisioned and the method becomes a no-op.
        """
        if not self.images_config["replication"]["enabled"]:
            return

        destinations = self._resolve_replication_destinations(
            self.images_config["replication"]["destinations"]
        )
        if not destinations:
            return

        ecr.CfnReplicationConfiguration(
            self,
            "GcoImageReplicationConfig",
            replication_configuration=ecr.CfnReplicationConfiguration.ReplicationConfigurationProperty(
                rules=[
                    ecr.CfnReplicationConfiguration.ReplicationRuleProperty(
                        destinations=[
                            ecr.CfnReplicationConfiguration.ReplicationDestinationProperty(
                                region=region,
                                registry_id=self.account,
                            )
                            for region in destinations
                        ],
                        repository_filters=[
                            ecr.CfnReplicationConfiguration.RepositoryFilterProperty(
                                filter="gco/",
                                filter_type="PREFIX_MATCH",
                            )
                        ],
                    )
                ]
            ),
        )

    def _create_image_lookup_lambda(self) -> None:
        """Create the lookup-or-create custom resource Lambda for image repos.

        The Lambda implements the adopt-or-create pattern for ECR repos
        under the project's ``gco/*`` prefix. It is invoked at the time
        ``cli images init`` registers a new repo with the global stack via
        a ``CustomResource``; the function itself is provisioned here so
        the ARN is stable across deploys.

        The Lambda's IAM role grants read/write access to ECR repository
        APIs scoped to the project's prefix, plus the standard basic
        execution policy for CloudWatch Logs.
        """
        project_name = self.config.get_project_name()

        # IAM role for the Lambda — minimal ECR + CloudWatch Logs permissions.
        # ECR repository APIs scope by repository name, not ARN, so the
        # ``gco/*`` prefix scope is enforced via the ARN pattern in the
        # policy resource list.
        repo_arn = f"arn:aws:ecr:*:{self.account}:repository/gco/*"

        self.image_lookup_lambda = lambda_.Function(
            self,
            "ImageLookupFunction",
            runtime=getattr(lambda_.Runtime, LAMBDA_PYTHON_RUNTIME),
            handler="handler.lambda_handler",
            code=lambda_.Code.from_asset("lambda/image-lookup"),
            timeout=Duration.minutes(5),
            description=(
                "Lookup-or-create custom resource handler for ECR "
                "repositories under the project's gco/* prefix."
            ),
        )

        assert self.image_lookup_lambda.role is not None
        self.image_lookup_lambda.role.add_to_principal_policy(
            iam.PolicyStatement(
                effect=iam.Effect.ALLOW,
                actions=[
                    "ecr:DescribeRepositories",
                    "ecr:CreateRepository",
                    "ecr:DeleteRepository",
                    "ecr:PutLifecyclePolicy",
                    "ecr:GetLifecyclePolicy",
                    "ecr:TagResource",
                    "ecr:ListTagsForResource",
                    "ecr:BatchDeleteImage",
                    "ecr:DescribeImages",
                    "ecr:ListImages",
                ],
                resources=[repo_arn],
            )
        )

        CfnOutput(
            self,
            "ImageLookupFunctionArn",
            value=self.image_lookup_lambda.function_arn,
            description=(
                "Lambda ARN for the lookup-or-create custom resource that "
                "manages ECR repositories under the gco/* prefix."
            ),
            export_name=f"{project_name}-image-lookup-function-arn",
        )

        # The ECR repository policy uses ``arn:aws:ecr:*:<account>:repository/gco/*``
        # which cdk-nag flags as ``AwsSolutions-IAM5`` because of the trailing
        # ``*``. The wildcard here is the documented IAM way to express
        # "every repository in this project's prefix", which is exactly the
        # blast radius we want for a Lambda whose contract is to manage
        # ECR repos under that prefix. Suppression is scoped to the specific
        # ARN pattern (and to all ECR Describe/Read action wildcards in
        # the policy below) rather than a blanket ``Resource::*`` bypass.
        #
        # cdk-nag reports this finding's account as the ``<AWS::AccountId>``
        # placeholder for an environment-agnostic synth, but as the concrete
        # account id for an environment-specific one (the ARN above is
        # hand-built from ``self.account``, which becomes a literal once the
        # stack has a resolved account). We author the ``appliesTo`` with the
        # placeholder; ``acknowledge_nag_findings`` additionally registers the
        # concrete-account rendering when the account is resolved, so the
        # acknowledgment matches in both cases.
        from gco.stacks.nag_suppressions import acknowledge_nag_findings

        acknowledge_nag_findings(
            self.image_lookup_lambda.role,
            [
                {
                    "id": "AwsSolutions-IAM5",
                    "reason": (
                        "The ImageLookupFunction's contract is to look up "
                        "or create any ECR repository under the project's "
                        "``gco/*`` prefix. The ARN pattern "
                        "``arn:aws:ecr:*:<account>:repository/gco/*`` is "
                        "the documented IAM way to express that scope: it "
                        "covers exactly the repositories the function is "
                        "allowed to touch and nothing else."
                    ),
                    "appliesTo": [
                        "Resource::arn:aws:ecr:*:<AWS::AccountId>:repository/gco/*",
                    ],
                },
            ],
        )
