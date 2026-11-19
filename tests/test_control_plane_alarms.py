"""Tests for gco/stacks/control_plane_alarms.py and where the control plane's alarms live.

CloudWatch evaluates an alarm only against metrics of its own Region, and an
alarm's SNS action must be a topic of that Region. The jobs table's alarms read
metrics of ``deployment_regions.global`` and the API Gateway and Lambda alarms
metrics of ``deployment_regions.api_gateway``. The monitoring stack creates a
group while that Region is its own (the default topology, one topic); otherwise
the global or API Gateway stack creates it with an alert topic of its own.
Before this, the monitoring stack created both groups wherever it was: the API
alarms never saw data when the API Gateway was in another Region, and a global
stack in another Region failed synthesis on the jobs table's alarms.

The topology tests synthesize the whole app (no workload Region, which keeps
them fast) through the same builder as the synthesis matrix, and run that
matrix's alarm Region check on the result.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import aws_cdk as cdk
import pytest
from aws_cdk import assertions
from aws_cdk import aws_sns as sns

from gco.stacks.control_plane_alarms import (
    DYNAMODB_ALARM_OPERATIONS,
    add_api_alarms,
    add_jobs_table_alarms,
    create_alert_topic,
)
from tests._cdk_app_builder import load_context, synthesize_app
from tests.test_cdk_synthesis_matrix import _alarm_region_problems

_MONITORING_REGION = "us-east-2"
_OTHER_REGION = "us-west-2"
_JOBS_TABLE_ALARMS = ("DynamoDBJobsThrottleAlarm", "DynamoDBJobsErrorsAlarm")
_API_ALARMS = (
    "ApiGateway5xxAlarm",
    "ApiGatewayHighLatencyAlarm",
    "ProxyLambdaErrorsAlarm",
    "ProxyLambdaThrottlesAlarm",
    "RotationLambdaErrorsAlarm",
    "CompositeApiErrors",
    "CompositeLambdaErrors",
    "ApiLambdaCompositeAlarm",
)
_ALARM_TYPES = ("AWS::CloudWatch::Alarm", "AWS::CloudWatch::CompositeAlarm")


def _stack(region: str = _MONITORING_REGION) -> cdk.Stack:
    return cdk.Stack(
        cdk.App(), "AlarmsTest", env=cdk.Environment(account="123456789012", region=region)
    )


def _alarms(template: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Alarm and composite-alarm properties keyed by construct id (logical id minus hash)."""
    return {
        logical_id[:-8]: resource.get("Properties", {})
        for logical_id, resource in template.get("Resources", {}).items()
        if resource["Type"] in _ALARM_TYPES
    }


def _topics(template: dict[str, Any]) -> list[str]:
    return [
        logical_id
        for logical_id, resource in template.get("Resources", {}).items()
        if resource["Type"] == "AWS::SNS::Topic"
    ]


class TestHelpers:
    """The alarm builders keep the thresholds the monitoring stack used."""

    def test_api_alarms_with_every_lambda(self) -> None:
        stack = _stack()
        topic = sns.Topic(stack, "Topic")
        add_api_alarms(
            stack,
            region=_MONITORING_REGION,
            api_name="gco-global-api",
            proxy_function_name="gco-proxy",
            rotation_function_name="gco-rotation",
            topic=topic,
        )
        alarms = _alarms(assertions.Template.from_stack(stack).to_json())
        assert set(alarms) == set(_API_ALARMS)
        expected = {
            "ApiGateway5xxAlarm": ("AWS/ApiGateway", "5XXError", 10, 2, 2),
            "ApiGatewayHighLatencyAlarm": ("AWS/ApiGateway", "Latency", 10000, 3, 2),
            "ProxyLambdaErrorsAlarm": ("AWS/Lambda", "Errors", 5, 2, 2),
            "ProxyLambdaThrottlesAlarm": ("AWS/Lambda", "Throttles", 1, 2, 2),
            "RotationLambdaErrorsAlarm": ("AWS/Lambda", "Errors", 1, 1, 1),
            "CompositeApiErrors": ("AWS/ApiGateway", "5XXError", 5, 2, None),
            "CompositeLambdaErrors": ("AWS/Lambda", "Errors", 3, 2, None),
        }
        for alarm_id, (namespace, metric, threshold, periods, datapoints) in expected.items():
            alarm = alarms[alarm_id]
            assert (alarm["Namespace"], alarm["MetricName"]) == (namespace, metric), alarm_id
            assert alarm["Threshold"] == threshold, alarm_id
            assert alarm["EvaluationPeriods"] == periods, alarm_id
            assert alarm.get("DatapointsToAlarm") == datapoints, alarm_id
            assert alarm["TreatMissingData"] == "notBreaching", alarm_id
        assert alarms["RotationLambdaErrorsAlarm"]["Period"] == 3600
        assert alarms["ApiGatewayHighLatencyAlarm"]["ExtendedStatistic"] == "p99"
        dimensions = {
            alarm_id: {d["Name"]: d["Value"] for d in alarms[alarm_id]["Dimensions"]}
            for alarm_id in expected
        }
        assert dimensions["ApiGateway5xxAlarm"] == {"ApiName": "gco-global-api"}
        assert dimensions["CompositeLambdaErrors"] == {"FunctionName": "gco-proxy"}
        assert dimensions["RotationLambdaErrorsAlarm"] == {"FunctionName": "gco-rotation"}
        # The composite pages once; its two inputs have no action of their own.
        for alarm_id, alarm in alarms.items():
            if alarm_id.startswith("Composite"):
                assert "AlarmActions" not in alarm, alarm_id
            else:
                assert len(alarm["AlarmActions"]) == 1, alarm_id
                assert alarm["AlarmActions"][0]["Ref"].startswith("Topic"), alarm_id

    @pytest.mark.parametrize(
        ("rotation", "expected"),
        [
            (None, {"ApiGateway5xxAlarm", "ApiGatewayHighLatencyAlarm"}),
            (
                "gco-rotation",
                {"ApiGateway5xxAlarm", "ApiGatewayHighLatencyAlarm", "RotationLambdaErrorsAlarm"},
            ),
        ],
    )
    def test_api_alarms_without_a_proxy(self, rotation: str | None, expected: set[str]) -> None:
        """Outside the commercial partition there is no proxy Lambda, so no proxy
        alarms and no composite; the monitoring stack built without an API stack
        has no rotation Lambda either."""
        stack = _stack()
        alarms = add_api_alarms(
            stack,
            region=_MONITORING_REGION,
            api_name="gco-global-api",
            proxy_function_name=None,
            rotation_function_name=rotation,
            topic=sns.Topic(stack, "Topic"),
        )
        assert len(alarms) == len(expected)
        assert set(_alarms(assertions.Template.from_stack(stack).to_json())) == expected

    def test_jobs_table_alarms_sum_every_operation(self) -> None:
        stack = _stack()
        add_jobs_table_alarms(
            stack, table_name="gco-jobs", region=_MONITORING_REGION, topic=sns.Topic(stack, "Topic")
        )
        alarms = _alarms(assertions.Template.from_stack(stack).to_json())
        assert set(alarms) == set(_JOBS_TABLE_ALARMS)
        for alarm_id, metric_name, periods in (
            ("DynamoDBJobsThrottleAlarm", "ThrottledRequests", 2),
            ("DynamoDBJobsErrorsAlarm", "SystemErrors", 1),
        ):
            alarm = alarms[alarm_id]
            assert alarm["EvaluationPeriods"] == alarm["DatapointsToAlarm"] == periods
            assert alarm["ComparisonOperator"] == "GreaterThanOrEqualToThreshold"
            operations = [
                {d["Name"]: d["Value"] for d in query["MetricStat"]["Metric"]["Dimensions"]}
                for query in alarm["Metrics"]
                if "MetricStat" in query
            ]
            assert operations == [
                {"TableName": "gco-jobs", "Operation": operation}
                for operation in DYNAMODB_ALARM_OPERATIONS
            ]
            assert {
                query["MetricStat"]["Metric"]["MetricName"]
                for query in alarm["Metrics"]
                if "MetricStat" in query
            } == {metric_name}

    def test_api_alarms_outside_their_metrics_region_do_not_synthesize(self) -> None:
        """Every metric names its Region, so CDK refuses the alarm anywhere else."""
        stack = _stack(_MONITORING_REGION)
        topic = sns.Topic(stack, "Topic")
        with pytest.raises(Exception, match="Cannot create an Alarm in region"):
            add_api_alarms(
                stack,
                region=_OTHER_REGION,
                api_name="gco-global-api",
                proxy_function_name=None,
                rotation_function_name=None,
                topic=topic,
            )

    def test_jobs_table_alarms_outside_their_metrics_region_do_not_synthesize(self) -> None:
        stack = _stack(_MONITORING_REGION)
        topic = sns.Topic(stack, "Topic")
        with pytest.raises(Exception, match="Cannot create an Alarm in region"):
            add_jobs_table_alarms(stack, table_name="gco-jobs", region=_OTHER_REGION, topic=topic)

    def test_alert_topic_is_tls_only_and_exported(self) -> None:
        stack = _stack()
        create_alert_topic(
            stack, "AlertTopic", display_name="GCO Test Alerts", output_id="TestAlertTopicArn"
        )
        template = assertions.Template.from_stack(stack)
        template.has_resource_properties("AWS::SNS::Topic", {"DisplayName": "GCO Test Alerts"})
        template.has_resource_properties(
            "AWS::SNS::TopicPolicy",
            {
                "PolicyDocument": {
                    "Statement": assertions.Match.array_with(
                        [
                            assertions.Match.object_like(
                                {
                                    "Effect": "Deny",
                                    "Condition": {"Bool": {"aws:SecureTransport": "false"}},
                                }
                            )
                        ]
                    )
                }
            },
        )
        (output,) = template.find_outputs("TestAlertTopicArn").values()
        assert output["Value"] == {"Ref": next(iter(template.find_resources("AWS::SNS::Topic")))}


#: (global Region, API Gateway Region) per topology; the monitoring stack is in
#: us-east-2 throughout.
_TOPOLOGIES = {
    "same-region": (_MONITORING_REGION, _MONITORING_REGION),
    "global-split": (_OTHER_REGION, _MONITORING_REGION),
    "api-gateway-split": (_MONITORING_REGION, _OTHER_REGION),
    "both-split": (_OTHER_REGION, _OTHER_REGION),
}


@pytest.fixture(scope="module", params=list(_TOPOLOGIES), ids=list(_TOPOLOGIES))
def topology(request: pytest.FixtureRequest) -> SimpleNamespace:
    """The whole app synthesized for one topology, templates read eagerly."""
    global_region, api_gateway_region = _TOPOLOGIES[request.param]
    app = cdk.App(
        context=load_context(
            {
                "deployment_regions": {
                    "global": global_region,
                    "api_gateway": api_gateway_region,
                    "monitoring": _MONITORING_REGION,
                    "regional": [],
                }
            }
        )
    )
    assembly = synthesize_app(app)
    stacks = [
        SimpleNamespace(
            stack_name=stack.stack_name,
            environment=SimpleNamespace(region=stack.environment.region),
            template=stack.template,
        )
        for stack in assembly.stacks
    ]
    return SimpleNamespace(
        name=request.param,
        global_region=global_region,
        api_gateway_region=api_gateway_region,
        stacks=stacks,
        templates={stack.stack_name: stack.template for stack in stacks},
    )


class TestPlacement:
    """Each alarm is created once, in the stack of its metrics' Region."""

    def test_each_alarm_lives_once_in_its_metrics_region(self, topology: SimpleNamespace) -> None:
        jobs_owner = (
            "gco-monitoring" if topology.global_region == _MONITORING_REGION else "gco-global"
        )
        api_owner = (
            "gco-monitoring"
            if topology.api_gateway_region == _MONITORING_REGION
            else "gco-api-gateway"
        )
        placed = {
            alarm_id: stack_name
            for stack_name, template in topology.templates.items()
            for alarm_id in _alarms(template)
            if alarm_id in _JOBS_TABLE_ALARMS + _API_ALARMS
        }
        counts = [
            alarm_id
            for template in topology.templates.values()
            for alarm_id in _alarms(template)
            if alarm_id in _JOBS_TABLE_ALARMS + _API_ALARMS
        ]
        assert sorted(counts) == sorted(_JOBS_TABLE_ALARMS + _API_ALARMS)
        assert placed == {
            **dict.fromkeys(_JOBS_TABLE_ALARMS, jobs_owner),
            **dict.fromkeys(_API_ALARMS, api_owner),
        }

    def test_alarm_region_check_passes(self, topology: SimpleNamespace) -> None:
        problems = _alarm_region_problems(
            topology, publisher_regions={"GCO/BackendTLS": topology.api_gateway_region}
        )
        assert problems == []

    def test_a_stack_has_its_own_topic_only_where_it_has_alarms_of_its_own(
        self, topology: SimpleNamespace
    ) -> None:
        templates = topology.templates
        global_split = topology.global_region != _MONITORING_REGION
        api_split = topology.api_gateway_region != _MONITORING_REGION
        assert len(_topics(templates["gco-monitoring"])) == 1
        assert "AlertTopicArn" in templates["gco-monitoring"]["Outputs"]
        for stack_name, split, output in (
            ("gco-global", global_split, "GlobalAlertTopicArn"),
            ("gco-api-gateway", api_split, "ApiGatewayAlertTopicArn"),
        ):
            assert len(_topics(templates[stack_name])) == int(split), stack_name
            assert (output in templates[stack_name].get("Outputs", {})) is split, stack_name
        if topology.name == "same-region":
            # The default topology keeps a single control-plane topic.
            assert sum(len(_topics(template)) for template in templates.values()) == 1

    def test_every_action_notifies_a_topic_of_the_same_stack(
        self, topology: SimpleNamespace
    ) -> None:
        for stack_name, template in topology.templates.items():
            topics = set(_topics(template))
            for alarm_id, alarm in _alarms(template).items():
                for action in alarm.get("AlarmActions", []):
                    assert action.get("Ref") in topics, (stack_name, alarm_id, action)
