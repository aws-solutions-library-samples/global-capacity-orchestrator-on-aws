"""CloudWatch alarms on the control plane's metrics, in the Region of those metrics.

CloudWatch evaluates an alarm only against metrics in the alarm's own Region,
and an alarm's SNS action must be a topic in that Region. The jobs table lives
in ``deployment_regions.global``; the API Gateway REST API, its proxy Lambda and
its secret-rotation Lambda live in ``deployment_regions.api_gateway``. Each
group of alarms below is created by:

- the monitoring stack, notifying its ``AlertTopicArn`` topic, when the group's
  Region is the monitoring Region. That is the default topology, which so keeps
  a single control-plane topic. (The global and API Gateway stacks deploy
  before the monitoring stack, so alarms of their own could not notify it.)
- otherwise the stack that owns the metrics: the global stack for the jobs
  table and the API Gateway stack for the API and its Lambdas, each with an
  alert topic of its own in its Region (:func:`create_alert_topic`), exported
  as ``GlobalAlertTopicArn`` or ``ApiGatewayAlertTopicArn``.

Jobs table (:func:`add_jobs_table_alarms`):

- ``DynamoDBJobsThrottleAlarm``: a throttled request in two consecutive
  5-minute periods.
- ``DynamoDBJobsErrorsAlarm``: a DynamoDB system error in a 5-minute period.

API (:func:`add_api_alarms`):

- ``ApiGateway5xxAlarm``: more than 10 5XX responses in two consecutive
  5-minute periods.
- ``ApiGatewayHighLatencyAlarm``: p99 latency above 10 seconds in two of three
  5-minute periods.
- ``RotationLambdaErrorsAlarm``: a secret-rotation error within an hour.
- Where the proxy Lambda exists (Global Accelerator, so the commercial ``aws``
  partition only): ``ProxyLambdaErrorsAlarm`` (more than 5 errors),
  ``ProxyLambdaThrottlesAlarm`` (a throttle), and ``ApiLambdaCompositeAlarm``,
  the composite of ``CompositeApiErrors`` and ``CompositeLambdaErrors``, which
  have no action of their own so one incident pages once.

Every metric names its Region explicitly, so CDK refuses to synthesize one of
these alarms in any other Region.
"""

from __future__ import annotations

from aws_cdk import CfnOutput, Duration, Stack
from aws_cdk import aws_cloudwatch as cloudwatch
from aws_cdk import aws_cloudwatch_actions as cw_actions
from aws_cdk import aws_sns as sns
from constructs import Construct

from gco.stacks.nag_suppressions import acknowledge_nag_findings

#: DynamoDB publishes ``ThrottledRequests`` and ``SystemErrors`` only per
#: ``TableName`` *and* ``Operation``; a metric with ``TableName`` alone never
#: has data. The alarms sum these operations (every operation GCO issues on the
#: jobs table, plus the batch and transaction calls). An alarm's metric math
#: may reference at most 10 metrics, which is exactly this list.
DYNAMODB_ALARM_OPERATIONS: tuple[str, ...] = (
    "GetItem",
    "PutItem",
    "UpdateItem",
    "DeleteItem",
    "Query",
    "Scan",
    "BatchGetItem",
    "BatchWriteItem",
    "TransactGetItems",
    "TransactWriteItems",
)

#: Reason shared by the SNS encryption findings of a control-plane alert topic.
_TOPIC_ENCRYPTION_REASON = (
    "The alert topic carries CloudWatch alarm notifications: alarm names, states, "
    "thresholds and metric values, no workload data. It uses SNS without a customer "
    "managed key, like the monitoring stack's alert topic."
)

#: Reason shared by the alarm-action findings of the composite inputs.
_COMPOSITE_INPUT_REASON = (
    "CompositeApiErrors and CompositeLambdaErrors are inputs of the "
    "ApiLambdaCompositeAlarm composite alarm, which notifies the alert topic; they "
    "have no action of their own so one incident pages once."
)

_FIVE_MINUTES = Duration.minutes(5)


def dynamodb_operations_sum(
    metric_name: str, table_name: str, *, region: str, label: str
) -> cloudwatch.MathExpression:
    """Sum of a per-operation DynamoDB metric over ``DYNAMODB_ALARM_OPERATIONS``.

    ``FILL(..., 0)`` gives every period a value even when no operation had a
    datapoint, so the sum never depends on all ten series being present.
    """
    return cloudwatch.MathExpression(
        expression="SUM(FILL(METRICS(), 0))",
        using_metrics={
            f"m{index}": cloudwatch.Metric(
                namespace="AWS/DynamoDB",
                metric_name=metric_name,
                dimensions_map={"TableName": table_name, "Operation": operation},
                statistic="Sum",
                period=_FIVE_MINUTES,
                region=region,
            )
            for index, operation in enumerate(DYNAMODB_ALARM_OPERATIONS)
        },
        label=label,
        period=_FIVE_MINUTES,
    )


def create_alert_topic(
    scope: Construct, construct_id: str, *, display_name: str, output_id: str
) -> sns.Topic:
    """A TLS-only SNS topic for alarm notifications, exported as output ``output_id``.

    For the stacks that own alarms outside the monitoring Region; the
    monitoring stack keeps its own ``GCOAlertTopic``.
    """
    topic = sns.Topic(scope, construct_id, display_name=display_name, enforce_ssl=True)
    acknowledge_nag_findings(
        topic,
        [
            {"id": "HIPAA.Security-SNSEncryptedKMS", "reason": _TOPIC_ENCRYPTION_REASON},
            {"id": "NIST.800.53.R5-SNSEncryptedKMS", "reason": _TOPIC_ENCRYPTION_REASON},
            {"id": "PCI.DSS.321-SNSEncryptedKMS", "reason": _TOPIC_ENCRYPTION_REASON},
        ],
    )
    CfnOutput(
        Stack.of(scope),
        output_id,
        value=topic.topic_arn,
        description=(
            f"SNS topic for the CloudWatch alarms of this stack ({display_name}); subscribe "
            "on-call alongside the monitoring stack's AlertTopicArn"
        ),
    )
    return topic


def _alarm(
    scope: Construct,
    alarm_id: str,
    *,
    metric: cloudwatch.IMetric,
    threshold: float,
    evaluation_periods: int,
    datapoints_to_alarm: int | None,
    description: str | None,
    topic: sns.ITopic | None,
    comparison: cloudwatch.ComparisonOperator = cloudwatch.ComparisonOperator.GREATER_THAN_THRESHOLD,
) -> cloudwatch.Alarm:
    """One alarm that treats missing data as not breaching and notifies ``topic``."""
    alarm = cloudwatch.Alarm(
        scope,
        alarm_id,
        alarm_description=description,
        metric=metric,
        threshold=threshold,
        comparison_operator=comparison,
        evaluation_periods=evaluation_periods,
        datapoints_to_alarm=datapoints_to_alarm,
        treat_missing_data=cloudwatch.TreatMissingData.NOT_BREACHING,
    )
    if topic is not None:
        alarm.add_alarm_action(cw_actions.SnsAction(topic))
    return alarm


def add_jobs_table_alarms(
    scope: Construct, *, table_name: str, region: str, topic: sns.ITopic
) -> list[cloudwatch.Alarm]:
    """The jobs table's throttling and system-error alarms, in ``scope``.

    ``region`` is the table's Region (``deployment_regions.global``).
    """
    return [
        _alarm(
            scope,
            "DynamoDBJobsThrottleAlarm",
            description="DynamoDB jobs table is being throttled",
            metric=dynamodb_operations_sum(
                "ThrottledRequests",
                table_name,
                region=region,
                label="Jobs table throttled requests (all operations)",
            ),
            threshold=1,
            comparison=cloudwatch.ComparisonOperator.GREATER_THAN_OR_EQUAL_TO_THRESHOLD,
            evaluation_periods=2,
            datapoints_to_alarm=2,
            topic=topic,
        ),
        _alarm(
            scope,
            "DynamoDBJobsErrorsAlarm",
            description="DynamoDB jobs table has system errors",
            metric=dynamodb_operations_sum(
                "SystemErrors",
                table_name,
                region=region,
                label="Jobs table system errors (all operations)",
            ),
            threshold=1,
            comparison=cloudwatch.ComparisonOperator.GREATER_THAN_OR_EQUAL_TO_THRESHOLD,
            evaluation_periods=1,
            datapoints_to_alarm=1,
            topic=topic,
        ),
    ]


def add_api_alarms(
    scope: Construct,
    *,
    region: str,
    api_name: str,
    proxy_function_name: str | None,
    rotation_function_name: str | None,
    topic: sns.ITopic,
) -> list[cloudwatch.Alarm | cloudwatch.CompositeAlarm]:
    """The API Gateway, proxy Lambda and secret-rotation alarms, in ``scope``.

    ``region`` is the API's Region (``deployment_regions.api_gateway``). The
    proxy alarms and the composite are skipped without a proxy Lambda, and the
    rotation alarm without a rotation Lambda.
    """

    def metric(
        namespace: str,
        metric_name: str,
        dimensions: dict[str, str],
        statistic: str,
        period: Duration = _FIVE_MINUTES,
    ) -> cloudwatch.Metric:
        return cloudwatch.Metric(
            namespace=namespace,
            metric_name=metric_name,
            dimensions_map=dimensions,
            statistic=statistic,
            period=period,
            region=region,
        )

    api = {"ApiName": api_name}
    alarms: list[cloudwatch.Alarm | cloudwatch.CompositeAlarm] = [
        _alarm(
            scope,
            "ApiGateway5xxAlarm",
            description="API Gateway has high 5XX error rate",
            metric=metric("AWS/ApiGateway", "5XXError", api, "Sum"),
            threshold=10,
            evaluation_periods=2,
            datapoints_to_alarm=2,
            topic=topic,
        ),
        _alarm(
            scope,
            "ApiGatewayHighLatencyAlarm",
            description="API Gateway has high latency",
            metric=metric("AWS/ApiGateway", "Latency", api, "p99"),
            threshold=10000,  # 10 seconds
            evaluation_periods=3,
            datapoints_to_alarm=2,
            topic=topic,
        ),
    ]
    if proxy_function_name is not None:
        proxy = {"FunctionName": proxy_function_name}
        alarms += [
            _alarm(
                scope,
                "ProxyLambdaErrorsAlarm",
                description="API Gateway proxy Lambda has errors",
                metric=metric("AWS/Lambda", "Errors", proxy, "Sum"),
                threshold=5,
                evaluation_periods=2,
                datapoints_to_alarm=2,
                topic=topic,
            ),
            _alarm(
                scope,
                "ProxyLambdaThrottlesAlarm",
                description="API Gateway proxy Lambda is being throttled",
                metric=metric("AWS/Lambda", "Throttles", proxy, "Sum"),
                threshold=1,
                comparison=cloudwatch.ComparisonOperator.GREATER_THAN_OR_EQUAL_TO_THRESHOLD,
                evaluation_periods=2,
                datapoints_to_alarm=2,
                topic=topic,
            ),
        ]
        # API Gateway and proxy errors at once are one incident; the inputs
        # carry no action so it pages once, through the composite.
        composite_inputs = [
            _alarm(
                scope,
                "CompositeApiErrors",
                description=None,
                metric=metric("AWS/ApiGateway", "5XXError", api, "Sum"),
                threshold=5,
                evaluation_periods=2,
                datapoints_to_alarm=None,
                topic=None,
            ),
            _alarm(
                scope,
                "CompositeLambdaErrors",
                description=None,
                metric=metric("AWS/Lambda", "Errors", proxy, "Sum"),
                threshold=3,
                evaluation_periods=2,
                datapoints_to_alarm=None,
                topic=None,
            ),
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
        composite = cloudwatch.CompositeAlarm(
            scope,
            "ApiLambdaCompositeAlarm",
            alarm_description="Critical: Both API Gateway and Lambda proxy have errors",
            alarm_rule=cloudwatch.AlarmRule.all_of(*composite_inputs),
        )
        composite.add_alarm_action(cw_actions.SnsAction(topic))
        alarms += [*composite_inputs, composite]
    if rotation_function_name is not None:
        alarms.append(
            _alarm(
                scope,
                "RotationLambdaErrorsAlarm",
                description="Secret rotation Lambda has errors",
                metric=metric(
                    "AWS/Lambda",
                    "Errors",
                    {"FunctionName": rotation_function_name},
                    "Sum",
                    period=Duration.hours(1),
                ),
                threshold=1,
                comparison=cloudwatch.ComparisonOperator.GREATER_THAN_OR_EQUAL_TO_THRESHOLD,
                evaluation_periods=1,
                datapoints_to_alarm=1,
                topic=topic,
            )
        )
    return alarms
