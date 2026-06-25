# Tests for the optional Historical Capacity Surface add-on folded into
# GCOGlobalStack (gated by historical.enabled). Synthesizes the global stack
# with the add-on enabled/disabled and asserts the capacity-history DynamoDB
# table (TTL, by-timestamp GSI), the poller Lambda env, the EventBridge
# schedule, and the DLQ are present only when enabled.

import aws_cdk as cdk
from aws_cdk import assertions

from gco.stacks.global_stack import GCOGlobalStack
from tests.test_regional_stack import MockConfigLoader


class _EnabledConfig(MockConfigLoader):
    """MockConfigLoader variant with the capacity-history add-on enabled."""

    def get_capacity_history_enabled(self):
        return True

    def get_capacity_history_config(self):
        return {
            "enabled": True,
            "retention_days": 90,
            "poll_interval_minutes": 15,
            "capacity_block_duration_hours": 24,
            "capacity_block_long_duration_hours": 1512,
            "watch_instance_types": ["g5.xlarge", "p5.48xlarge"],
            "enabled_regions": [],
        }


def _synth(cfg):
    app = cdk.App()
    stack = GCOGlobalStack(app, "id", config=cfg)
    return assertions.Template.from_stack(stack)


def _capacity_tables(template):
    out = {}
    for lid, res in template.find_resources("AWS::DynamoDB::Table").items():
        name = res.get("Properties", {}).get("TableName")
        if isinstance(name, str) and name.endswith("-capacity-history"):
            out[lid] = res
    return out


def _capacity_lambdas(template):
    out = {}
    for lid, res in template.find_resources("AWS::Lambda::Function").items():
        env = res.get("Properties", {}).get("Environment", {}).get("Variables", {})
        if isinstance(env, dict) and "CAPACITY_HISTORY_TABLE_NAME" in env:
            out[lid] = res
    return out


class TestCapacityPollerAddOnEnabled:
    def test_history_table_present_with_ttl_and_gsi(self):
        template = _synth(_EnabledConfig())
        tables = _capacity_tables(template)
        assert len(tables) == 1
        props = next(iter(tables.values()))["Properties"]
        assert props["TimeToLiveSpecification"] == {"AttributeName": "ttl", "Enabled": True}
        gsis = props.get("GlobalSecondaryIndexes", [])
        assert any(g.get("IndexName") == "by-timestamp" for g in gsis)

    def test_poller_lambda_present(self):
        template = _synth(_EnabledConfig())
        lambdas = _capacity_lambdas(template)
        assert len(lambdas) == 1
        assert next(iter(lambdas.values()))["Properties"]["Handler"] == "handler.lambda_handler"

    def test_poller_lambda_has_block_duration_env(self):
        template = _synth(_EnabledConfig())
        lambdas = _capacity_lambdas(template)
        env = next(iter(lambdas.values()))["Properties"]["Environment"]["Variables"]
        assert env["CAPACITY_BLOCK_DURATION_HOURS"] == "24"
        assert env["CAPACITY_BLOCK_LONG_DURATION_HOURS"] == "1512"

    def test_schedule_rule_present(self):
        template = _synth(_EnabledConfig())
        rules = template.find_resources("AWS::Events::Rule")
        exprs = [r.get("Properties", {}).get("ScheduleExpression") for r in rules.values()]
        assert "rate(15 minutes)" in exprs

    def test_dlq_present(self):
        template = _synth(_EnabledConfig())
        queues = template.find_resources("AWS::SQS::Queue")
        assert len(queues) >= 1


class TestCapacityPollerAddOnDisabled:
    def test_no_capacity_history_table(self):
        template = _synth(MockConfigLoader())
        assert _capacity_tables(template) == {}

    def test_no_poller_lambda(self):
        template = _synth(MockConfigLoader())
        assert _capacity_lambdas(template) == {}
