"""Per-Region CloudWatch alarms for one regional stack.

CloudWatch evaluates an alarm only against metrics in the alarm's own Region,
and an alarm's SNS action must be a topic in that Region. The job queues, the
EKS cluster's Container Insights metrics and the manifest processor's custom
metrics all live in the workload Region, so their alarms are created here,
inside the regional stack, together with an alert topic in the same Region.
The monitoring stack keeps the cross-Region dashboard and the alarms whose
metrics live in its own Region (API Gateway, the API Lambdas, DynamoDB).

Alarms (thresholds unchanged from the monitoring stack that used to own them):

- ``SqsOldMessageAlarm``: the oldest job message is more than an hour old.
- ``SqsDlqAlarm``: the job dead-letter queue holds a message.
- ``EksHighCpuAlarm`` / ``EksHighMemoryAlarm``: node CPU above 80% or memory
  above 85% (Container Insights, cluster-wide average).
- ``ManifestHighFailureRateAlarm``: more than 10 manifest failures in 5 minutes.
- ``RegionalCriticalAlarm``: composite of CPU and memory both above 90%
  (``CompositeEksCpu`` and ``CompositeEksMemory``, which have no action of
  their own).

Every alarm except the two composite inputs notifies ``alert_topic``; the
regional stack exports its ARN as the ``RegionalAlertTopicArn`` output.
"""

from __future__ import annotations

from aws_cdk import CfnOutput, Duration, Stack
from aws_cdk import aws_cloudwatch as cloudwatch
from aws_cdk import aws_cloudwatch_actions as cw_actions
from aws_cdk import aws_sns as sns
from aws_cdk import aws_sqs as sqs
from constructs import Construct

from gco.stacks.nag_suppressions import acknowledge_nag_findings

#: Reason shared by the SNS encryption findings of the regional alert topic.
_TOPIC_ENCRYPTION_REASON = (
    "The regional alert topic carries CloudWatch alarm notifications: alarm names, "
    "states, thresholds and metric values, no workload data. It uses SNS without a "
    "customer managed key, like the monitoring stack's alert topic."
)

#: Reason shared by the alarm-action findings of the composite inputs.
_COMPOSITE_INPUT_REASON = (
    "CompositeEksCpu and CompositeEksMemory are inputs of the RegionalCriticalAlarm "
    "composite alarm, which notifies the regional alert topic; they have no action "
    "of their own so one incident pages once."
)


class RegionalAlarms(Construct):
    """CloudWatch alarms and their alert topic, in the Region of their metrics.

    Attributes:
        alert_topic: SNS topic every alarm here notifies.
        alarms: The metric and composite alarms, keyed by construct id.
    """

    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        region: str,
        cluster_name: str,
        job_queue: sqs.IQueue,
        job_dlq: sqs.IQueue,
    ) -> None:
        super().__init__(scope, construct_id)
        self.alarms: dict[str, cloudwatch.Alarm | cloudwatch.CompositeAlarm] = {}

        self.alert_topic = sns.Topic(
            self,
            "AlertTopic",
            display_name=f"GCO (Global Capacity Orchestrator on AWS) Alerts for {region}",
            enforce_ssl=True,
        )
        acknowledge_nag_findings(
            self.alert_topic,
            [
                {"id": "HIPAA.Security-SNSEncryptedKMS", "reason": _TOPIC_ENCRYPTION_REASON},
                {"id": "NIST.800.53.R5-SNSEncryptedKMS", "reason": _TOPIC_ENCRYPTION_REASON},
                {"id": "PCI.DSS.321-SNSEncryptedKMS", "reason": _TOPIC_ENCRYPTION_REASON},
            ],
        )
        action = cw_actions.SnsAction(self.alert_topic)

        def alarm(
            alarm_id: str,
            *,
            metric: cloudwatch.Metric,
            threshold: float,
            evaluation_periods: int,
            datapoints_to_alarm: int | None,
            description: str | None,
            comparison: cloudwatch.ComparisonOperator = (
                cloudwatch.ComparisonOperator.GREATER_THAN_THRESHOLD
            ),
            notify: bool = True,
        ) -> cloudwatch.Alarm:
            created = cloudwatch.Alarm(
                self,
                alarm_id,
                alarm_description=description,
                metric=metric,
                threshold=threshold,
                comparison_operator=comparison,
                evaluation_periods=evaluation_periods,
                datapoints_to_alarm=datapoints_to_alarm,
                treat_missing_data=cloudwatch.TreatMissingData.NOT_BREACHING,
            )
            if notify:
                created.add_alarm_action(action)
            self.alarms[alarm_id] = created
            return created

        five_minutes = Duration.minutes(5)

        # Stuck jobs: the oldest message has waited more than an hour.
        alarm(
            "SqsOldMessageAlarm",
            description=f"SQS queue in {region} has old messages (potential stuck jobs)",
            metric=cloudwatch.Metric(
                namespace="AWS/SQS",
                metric_name="ApproximateAgeOfOldestMessage",
                dimensions_map={"QueueName": job_queue.queue_name},
                statistic="Maximum",
                period=five_minutes,
            ),
            threshold=3600,
            evaluation_periods=2,
            datapoints_to_alarm=2,
        )
        alarm(
            "SqsDlqAlarm",
            description=f"SQS dead letter queue in {region} has messages",
            metric=cloudwatch.Metric(
                namespace="AWS/SQS",
                metric_name="ApproximateNumberOfMessagesVisible",
                dimensions_map={"QueueName": job_dlq.queue_name},
                statistic="Sum",
                period=five_minutes,
            ),
            threshold=1,
            comparison=cloudwatch.ComparisonOperator.GREATER_THAN_OR_EQUAL_TO_THRESHOLD,
            evaluation_periods=1,
            datapoints_to_alarm=1,
        )

        def node_metric(metric_name: str) -> cloudwatch.Metric:
            return cloudwatch.Metric(
                namespace="ContainerInsights",
                metric_name=metric_name,
                dimensions_map={"ClusterName": cluster_name},
                statistic="Average",
                period=five_minutes,
            )

        alarm(
            "EksHighCpuAlarm",
            description=f"EKS cluster {cluster_name} has high CPU utilization",
            metric=node_metric("node_cpu_utilization"),
            threshold=80,
            evaluation_periods=3,
            datapoints_to_alarm=2,
        )
        alarm(
            "EksHighMemoryAlarm",
            description=f"EKS cluster {cluster_name} has high memory utilization",
            metric=node_metric("node_memory_utilization"),
            threshold=85,
            evaluation_periods=3,
            datapoints_to_alarm=2,
        )
        alarm(
            "ManifestHighFailureRateAlarm",
            description=f"Manifest processor in {region} has high failure rate",
            metric=cloudwatch.Metric(
                namespace="GCO/ManifestProcessor",
                metric_name="ManifestFailures",
                dimensions_map={"ClusterName": cluster_name, "Region": region},
                statistic="Sum",
                period=five_minutes,
            ),
            threshold=10,
            evaluation_periods=2,
            datapoints_to_alarm=2,
        )

        # Both CPU and memory above 90% at once is one critical incident; the
        # inputs carry no action so it pages once, through the composite.
        composite_inputs = [
            alarm(
                input_id,
                description=None,
                metric=node_metric(metric_name),
                threshold=90,
                evaluation_periods=2,
                datapoints_to_alarm=None,
                notify=False,
            )
            for input_id, metric_name in (
                ("CompositeEksCpu", "node_cpu_utilization"),
                ("CompositeEksMemory", "node_memory_utilization"),
            )
        ]
        for composite_input in composite_inputs:
            acknowledge_nag_findings(
                composite_input,
                [
                    {
                        "id": "HIPAA.Security-CloudWatchAlarmAction",
                        "reason": _COMPOSITE_INPUT_REASON,
                    },
                    {
                        "id": "NIST.800.53.R5-CloudWatchAlarmAction",
                        "reason": _COMPOSITE_INPUT_REASON,
                    },
                ],
            )
        critical = cloudwatch.CompositeAlarm(
            self,
            "RegionalCriticalAlarm",
            alarm_description=f"Critical: Multiple issues detected in {region}",
            alarm_rule=cloudwatch.AlarmRule.all_of(*composite_inputs),
        )
        critical.add_alarm_action(action)
        self.alarms["RegionalCriticalAlarm"] = critical

        CfnOutput(
            Stack.of(self),
            "RegionalAlertTopicArn",
            value=self.alert_topic.topic_arn,
            description=(
                f"SNS topic for the CloudWatch alarms of {region}; subscribe on-call "
                "alongside the monitoring stack's AlertTopicArn"
            ),
        )
