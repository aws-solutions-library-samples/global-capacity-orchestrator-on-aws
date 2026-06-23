"""Capacity poller stack for the Historical Capacity Surface (optional).

Instantiated only when ``historical.enabled=true`` in cdk.json. When the toggle
is false (the default), app.py skips creating it, so cdk synth emits no DynamoDB
table, Lambda, or EventBridge rule for this feature.

Resources:
    - A DynamoDB ``{project}-capacity-history`` table with the time-series schema
      (pk = ``{instance_type}#{region}``, sk = ISO timestamp), a ``by-timestamp``
      GSI for cross-region queries, ``ttl`` auto-expiry, point-in-time recovery,
      and AWS-managed encryption.
    - A poller Lambda (``lambda/capacity-poller``) that snapshots capacity signals
      for the watched instance types across the enabled regions.
    - An EventBridge rule that invokes the poller on a fixed cadence (default every
      15 minutes), with a dead-letter queue for failed invocations.
    - A least-privilege IAM role: DynamoDB write on the history table only, plus
      the read-only EC2 capacity APIs the poller calls.
"""

from __future__ import annotations

from typing import Any

from aws_cdk import (
    CfnOutput,
    Duration,
    RemovalPolicy,
    Stack,
)
from aws_cdk import aws_dynamodb as dynamodb
from aws_cdk import aws_events as events
from aws_cdk import aws_events_targets as events_targets
from aws_cdk import aws_iam as iam
from aws_cdk import aws_lambda as lambda_
from aws_cdk import aws_sqs as sqs
from constructs import Construct

from gco.config.config_loader import ConfigLoader
from gco.stacks.constants import LAMBDA_PYTHON_RUNTIME


class GCOCapacityPollerStack(Stack):
    """Time-series capacity poller and history table (feature-flagged)."""

    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        config: ConfigLoader,
        **kwargs: Any,
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)

        self.config = config
        project_name = config.get_project_name()
        historical_config = config.get_capacity_history_config()

        retention_days = int(historical_config["retention_days"])
        poll_interval_minutes = int(historical_config["poll_interval_minutes"])
        watch_instance_types = list(historical_config["watch_instance_types"])
        enabled_regions = list(historical_config["enabled_regions"]) or config.get_regions()

        self.history_table = self._create_history_table(project_name)
        poller_role = self._create_poller_role()
        self.poller_lambda = self._create_poller_lambda(
            poller_role,
            retention_days,
            watch_instance_types,
            enabled_regions,
        )
        poller_dlq = self._create_dlq()
        self.poller_rule = self._create_schedule(project_name, poll_interval_minutes, poller_dlq)
        self._create_outputs(project_name)
        self._apply_nag_suppressions(poller_role, poller_dlq)

    def _create_history_table(self, project_name: str) -> dynamodb.Table:
        table = dynamodb.Table(
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
        # GSI for cross-region queries of one instance type over time.
        table.add_global_secondary_index(
            index_name="by-timestamp",
            partition_key=dynamodb.Attribute(
                name="instance_type", type=dynamodb.AttributeType.STRING
            ),
            sort_key=dynamodb.Attribute(name="sk", type=dynamodb.AttributeType.STRING),
            projection_type=dynamodb.ProjectionType.ALL,
        )
        return table

    def _create_poller_role(self) -> iam.Role:
        role = iam.Role(
            self,
            "CapacityPollerRole",
            assumed_by=iam.ServicePrincipal("lambda.amazonaws.com"),
            managed_policies=[
                iam.ManagedPolicy.from_aws_managed_policy_name(
                    "service-role/AWSLambdaBasicExecutionRole"
                )
            ],
        )
        # DynamoDB write scoped to the history table and its indexes.
        role.add_to_policy(
            iam.PolicyStatement(
                effect=iam.Effect.ALLOW,
                actions=["dynamodb:PutItem", "dynamodb:BatchWriteItem"],
                resources=[
                    self.history_table.table_arn,
                    f"{self.history_table.table_arn}/index/*",
                ],
            )
        )
        # Read-only EC2 capacity APIs. These describe/get actions do not support
        # resource-level scoping and require a wildcard resource.
        role.add_to_policy(
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
        return role

    def _create_poller_lambda(
        self,
        role: iam.Role,
        retention_days: int,
        watch_instance_types: list[str],
        enabled_regions: list[str],
    ) -> lambda_.Function:
        return lambda_.Function(
            self,
            "CapacityPollerFunction",
            runtime=getattr(lambda_.Runtime, LAMBDA_PYTHON_RUNTIME),
            handler="handler.lambda_handler",
            code=lambda_.Code.from_asset("lambda/capacity-poller"),
            timeout=Duration.minutes(14),
            memory_size=256,
            role=role,
            environment={
                "CAPACITY_HISTORY_TABLE_NAME": self.history_table.table_name,
                "WATCH_INSTANCE_TYPES": ",".join(watch_instance_types),
                "ENABLED_REGIONS": ",".join(enabled_regions),
                "CAPACITY_HISTORY_RETENTION_DAYS": str(retention_days),
            },
            tracing=lambda_.Tracing.ACTIVE,
        )

    def _create_dlq(self) -> sqs.Queue:
        return sqs.Queue(
            self,
            "CapacityPollerRuleDlq",
            retention_period=Duration.days(14),
            enforce_ssl=True,
            encryption=sqs.QueueEncryption.SQS_MANAGED,
            removal_policy=RemovalPolicy.DESTROY,
        )

    def _create_schedule(
        self,
        project_name: str,
        poll_interval_minutes: int,
        dlq: sqs.Queue,
    ) -> events.Rule:
        return events.Rule(
            self,
            "CapacityPollerSchedule",
            description=(
                f"Capacity poller for {project_name} history surface "
                f"(every {poll_interval_minutes} min)"
            ),
            schedule=events.Schedule.rate(Duration.minutes(poll_interval_minutes)),
            targets=[
                events_targets.LambdaFunction(
                    self.poller_lambda,
                    dead_letter_queue=dlq,
                    retry_attempts=2,
                )
            ],
        )

    def _create_outputs(self, project_name: str) -> None:
        CfnOutput(
            self,
            "CapacityHistoryTableName",
            value=self.history_table.table_name,
            description="DynamoDB table name for historical capacity snapshots",
            export_name=f"{project_name}-capacity-history-table-name",
        )
        CfnOutput(
            self,
            "CapacityHistoryTableArn",
            value=self.history_table.table_arn,
            description="DynamoDB table ARN for historical capacity snapshots",
            export_name=f"{project_name}-capacity-history-table-arn",
        )

    def _apply_nag_suppressions(self, poller_role: iam.Role, poller_dlq: sqs.Queue) -> None:
        from cdk_nag import NagSuppressions

        NagSuppressions.add_resource_suppressions(
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
                },
            ],
            apply_to_children=True,
        )
        NagSuppressions.add_resource_suppressions(
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
            ],
        )
