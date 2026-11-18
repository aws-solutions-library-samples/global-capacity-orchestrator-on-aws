"""Tests for gco/stacks/regional_alarms.RegionalAlarms and where per-Region alarms live.

CloudWatch evaluates an alarm only against metrics in the alarm's own Region,
and an alarm's SNS action must be in that Region too. The monitoring stack used
to create every workload Region's queue, cluster and manifest-processor alarms
in the monitoring Region, where those metrics do not exist, so they never
fired. The alarms now live in each regional stack with an alert topic of their
own; these tests pin the construct, its wiring into the regional stack, and
that the monitoring stack no longer creates them.
"""

from __future__ import annotations

from typing import Any

import aws_cdk as cdk
from aws_cdk import assertions
from aws_cdk import aws_sqs as sqs

from gco.stacks.regional_alarms import RegionalAlarms

_REGION = "us-east-1"
_CLUSTER = "gco-us-east-1"

#: Namespaces whose metrics are published in the workload Region.
_REGIONAL_NAMESPACES = {"AWS/SQS", "ContainerInsights", "GCO/ManifestProcessor"}


def _construct_template() -> tuple[cdk.Stack, assertions.Template]:
    app = cdk.App()
    stack = cdk.Stack(
        app, "RegionalAlarmsTest", env=cdk.Environment(account="123456789012", region=_REGION)
    )
    queue = sqs.Queue(stack, "JobQueue", queue_name="gco-jobs-us-east-1")
    dlq = sqs.Queue(stack, "JobDlq", queue_name="gco-jobs-dlq-us-east-1")
    RegionalAlarms(
        stack,
        "RegionalAlarms",
        region=_REGION,
        cluster_name=_CLUSTER,
        job_queue=queue,
        job_dlq=dlq,
    )
    return stack, assertions.Template.from_stack(stack)


def _alarms_by_id(template: assertions.Template) -> dict[str, dict[str, Any]]:
    """Alarm properties keyed by construct id (the logical id without its hash)."""
    return {
        logical_id.removeprefix("RegionalAlarms")[:-8]: resource["Properties"]
        for logical_id, resource in template.find_resources("AWS::CloudWatch::Alarm").items()
    }


def _topic_ref(template: assertions.Template) -> dict[str, str]:
    (topic_id,) = template.find_resources("AWS::SNS::Topic")
    return {"Ref": topic_id}


def test_alarms_keep_the_monitoring_stack_thresholds_and_notify_the_regional_topic() -> None:
    _stack, template = _construct_template()
    alarms = _alarms_by_id(template)
    topic = _topic_ref(template)
    expected = {
        "SqsOldMessageAlarm": ("AWS/SQS", "ApproximateAgeOfOldestMessage", 3600, 2, 2),
        "SqsDlqAlarm": ("AWS/SQS", "ApproximateNumberOfMessagesVisible", 1, 1, 1),
        "EksHighCpuAlarm": ("ContainerInsights", "node_cpu_utilization", 80, 3, 2),
        "EksHighMemoryAlarm": ("ContainerInsights", "node_memory_utilization", 85, 3, 2),
        "ManifestHighFailureRateAlarm": ("GCO/ManifestProcessor", "ManifestFailures", 10, 2, 2),
        "CompositeEksCpu": ("ContainerInsights", "node_cpu_utilization", 90, 2, None),
        "CompositeEksMemory": ("ContainerInsights", "node_memory_utilization", 90, 2, None),
    }
    assert set(alarms) == set(expected)
    for alarm_id, (namespace, metric, threshold, periods, datapoints) in expected.items():
        properties = alarms[alarm_id]
        assert properties["Namespace"] == namespace, alarm_id
        assert properties["MetricName"] == metric, alarm_id
        assert properties["Threshold"] == threshold, alarm_id
        assert properties["EvaluationPeriods"] == periods, alarm_id
        assert properties.get("DatapointsToAlarm") == datapoints, alarm_id
        assert properties["TreatMissingData"] == "notBreaching", alarm_id
        if alarm_id.startswith("Composite"):
            assert "AlarmActions" not in properties, "composite inputs page through the composite"
        else:
            assert properties["AlarmActions"] == [topic], alarm_id

    assert {
        d["Name"]: d["Value"] for d in alarms["ManifestHighFailureRateAlarm"]["Dimensions"]
    } == {
        "ClusterName": _CLUSTER,
        "Region": _REGION,
    }
    assert alarms["EksHighCpuAlarm"]["Dimensions"] == [{"Name": "ClusterName", "Value": _CLUSTER}]
    assert alarms["SqsDlqAlarm"]["ComparisonOperator"] == "GreaterThanOrEqualToThreshold"
    assert alarms["SqsOldMessageAlarm"]["Statistic"] == "Maximum"


def test_composite_alarm_combines_both_inputs_and_notifies_the_topic() -> None:
    _stack, template = _construct_template()
    (composite,) = template.find_resources("AWS::CloudWatch::CompositeAlarm").values()
    properties = composite["Properties"]
    assert properties["AlarmDescription"] == f"Critical: Multiple issues detected in {_REGION}"
    assert properties["AlarmActions"] == [_topic_ref(template)]
    rule = str(properties["AlarmRule"])
    assert "CompositeEksCpu" in rule and "CompositeEksMemory" in rule and "AND" in rule


def test_topic_enforces_tls_and_its_arn_is_a_stack_output() -> None:
    _stack, template = _construct_template()
    template.has_resource_properties(
        "AWS::SNS::Topic",
        {"DisplayName": f"GCO (Global Capacity Orchestrator on AWS) Alerts for {_REGION}"},
    )
    (policy,) = template.find_resources("AWS::SNS::TopicPolicy").values()
    statements = policy["Properties"]["PolicyDocument"]["Statement"]
    assert any(
        statement["Effect"] == "Deny"
        and statement["Condition"] == {"Bool": {"aws:SecureTransport": "false"}}
        for statement in statements
    )
    template.has_output("RegionalAlertTopicArn", {"Value": _topic_ref(template)})


def test_regional_stack_creates_its_alarms_in_its_own_region() -> None:
    """The real regional stack wires the construct to its own queues and cluster."""
    from tests.test_regional_stack_feature_gap_coverage import _synthesize

    stack, template = _synthesize(
        feature_rich=False,
        global_accelerator=True,
        logical_name="regional-alarms-wiring",
    )
    assert isinstance(stack.regional_alarms, RegionalAlarms)
    alarms = template.find_resources("AWS::CloudWatch::Alarm")
    namespaces = {resource["Properties"]["Namespace"] for resource in alarms.values()}
    assert namespaces >= _REGIONAL_NAMESPACES
    queue_refs = {
        json_ref["Value"]["Fn::GetAtt"][0]
        for resource in alarms.values()
        if resource["Properties"]["Namespace"] == "AWS/SQS"
        for json_ref in resource["Properties"]["Dimensions"]
    }
    queues = template.find_resources("AWS::SQS::Queue")
    assert queue_refs <= set(queues) and len(queue_refs) == 2
    template.has_output("RegionalAlertTopicArn", {})
    assert len(template.find_resources("AWS::CloudWatch::CompositeAlarm")) == 1


def test_monitoring_stack_creates_no_alarm_on_workload_region_metrics() -> None:
    """Regression: these alarms sat in the monitoring Region and never saw data."""
    from gco.stacks.monitoring_stack import GCOMonitoringStack
    from tests.test_monitoring_stack import (
        MockConfigLoader,
        _alarm_namespaces,
        create_mock_api_gateway_stack,
        create_mock_global_stack,
        create_mock_regional_stack,
    )

    stack = GCOMonitoringStack(
        cdk.App(),
        "MonitoringWithoutRegionalAlarms",
        config=MockConfigLoader(cost_monitoring_enabled=False),
        global_stack=create_mock_global_stack(),
        regional_stacks=[
            create_mock_regional_stack("us-east-1"),
            create_mock_regional_stack("us-west-2"),
        ],
        api_gateway_stack=create_mock_api_gateway_stack(),
        env=cdk.Environment(account="123456789012", region="us-east-2"),
    )
    template = assertions.Template.from_stack(stack)
    alarms = template.find_resources("AWS::CloudWatch::Alarm")
    assert alarms, "the monitoring Region's own alarms remain"
    namespaces = {
        namespace for resource in alarms.values() for namespace in _alarm_namespaces(resource)
    }
    assert not namespaces & _REGIONAL_NAMESPACES
    composites = template.find_resources("AWS::CloudWatch::CompositeAlarm")
    assert [logical_id[:-8] for logical_id in composites] == ["ApiLambdaCompositeAlarm"]
