# Tests for gco/stacks/capacity_poller_stack.py -- feature-flagged history poller stack.
# Synthesizes against a stub config and asserts the DynamoDB table (TTL, GSI), poller
# Lambda env, EventBridge schedule, DLQ, and least-privilege IAM actions.

import aws_cdk as cdk
import pytest
from aws_cdk import assertions


class StubConfig:
    def get_project_name(self):
        return "gco-test"

    def get_regions(self):
        return ["us-east-1"]

    def get_capacity_history_config(self):
        return {
            "enabled": True,
            "retention_days": 90,
            "poll_interval_minutes": 15,
            "watch_instance_types": ["g5.xlarge", "p5.48xlarge"],
            "enabled_regions": [],
        }


@pytest.fixture(scope="module")
def template():
    from gco.stacks.capacity_poller_stack import GCOCapacityPollerStack

    app = cdk.App()
    stack = GCOCapacityPollerStack(
        app, "t", config=StubConfig(), env=cdk.Environment(region="us-east-2")
    )
    return assertions.Template.from_stack(stack)


def _all_policy_actions(template):
    actions = set()
    for policy in template.find_resources("AWS::IAM::Policy").values():
        for stmt in policy["Properties"]["PolicyDocument"]["Statement"]:
            act = stmt.get("Action", [])
            if isinstance(act, str):
                act = [act]
            actions.update(act)
    return actions


class TestResourceCounts:
    def test_counts(self, template):
        template.resource_count_is("AWS::DynamoDB::Table", 1)
        template.resource_count_is("AWS::Lambda::Function", 1)
        template.resource_count_is("AWS::Events::Rule", 1)
        template.resource_count_is("AWS::SQS::Queue", 1)


class TestHistoryTable:
    def test_ttl_and_billing(self, template):
        template.has_resource_properties(
            "AWS::DynamoDB::Table",
            {
                "BillingMode": "PAY_PER_REQUEST",
                "TimeToLiveSpecification": {"AttributeName": "ttl", "Enabled": True},
            },
        )

    def test_by_timestamp_gsi(self, template):
        template.has_resource_properties(
            "AWS::DynamoDB::Table",
            {
                "GlobalSecondaryIndexes": assertions.Match.array_with(
                    [
                        assertions.Match.object_like(
                            {
                                "IndexName": "by-timestamp",
                                "KeySchema": assertions.Match.array_with(
                                    [{"AttributeName": "instance_type", "KeyType": "HASH"}]
                                ),
                            }
                        )
                    ]
                )
            },
        )


class TestPollerLambda:
    def test_handler_and_environment(self, template):
        template.has_resource_properties(
            "AWS::Lambda::Function",
            {
                "Handler": "handler.lambda_handler",
                "Environment": {
                    "Variables": assertions.Match.object_like(
                        {
                            "CAPACITY_HISTORY_TABLE_NAME": assertions.Match.any_value(),
                            "WATCH_INSTANCE_TYPES": "g5.xlarge,p5.48xlarge",
                            "ENABLED_REGIONS": "us-east-1",
                            "CAPACITY_HISTORY_RETENTION_DAYS": "90",
                        }
                    )
                },
            },
        )


class TestSchedule:
    def test_rate_expression(self, template):
        template.has_resource_properties(
            "AWS::Events::Rule", {"ScheduleExpression": "rate(15 minutes)"}
        )


class TestIamLeastPrivilege:
    def test_actions_present(self, template):
        actions = _all_policy_actions(template)
        assert "ec2:GetSpotPlacementScores" in actions
        assert "dynamodb:PutItem" in actions
