# Tests for the optional Historical Capacity Surface add-on folded into
# GCOGlobalStack (gated by historical.enabled). Synthesizes the global stack
# with the add-on enabled/disabled and asserts the capacity-history DynamoDB
# table (TTL, by-timestamp GSI), the poller Lambda env (including the
# serialized SPS target-capacity mapping and instance-pool catalog), the
# EventBridge schedule, and the DLQ are present only when enabled.

import json

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
            "spot_score_target_capacities": [1, 10, 50],
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

    def test_target_capacities_env_derives_from_the_history_exports(self):
        # The env value is the configured capacities paired with field names
        # from cli/capacity/history.py — the naming rule's single source of
        # truth — not a literal maintained in the stack.
        from cli.capacity.history import metric_field_for_target_capacity

        template = _synth(_EnabledConfig())
        env = next(iter(_capacity_lambdas(template).values()))["Properties"]["Environment"][
            "Variables"
        ]
        parsed = json.loads(env["SPOT_SCORE_TARGET_CAPACITIES"])
        assert parsed == [
            {
                "target_capacity": capacity,
                "metric_field": metric_field_for_target_capacity(capacity),
            }
            for capacity in (1, 10, 50)
        ]

    def test_instance_pools_env_serializes_the_catalog_in_priority_order(self):
        from scripts.accelerator_catalog import INSTANCE_POOLS

        template = _synth(_EnabledConfig())
        env = next(iter(_capacity_lambdas(template).values()))["Properties"]["Environment"][
            "Variables"
        ]
        parsed = json.loads(env["INSTANCE_POOLS"])
        assert parsed == [
            {"name": pool.name, "members": list(pool.members)} for pool in INSTANCE_POOLS
        ]

    def test_pool_env_round_trips_through_the_handler_parsers(self):
        # Contract test: what the stack serializes, the poller must parse —
        # covering shape, the three-member minimum, and unique pool names.
        from tests._lambda_imports import load_lambda_module

        handler = load_lambda_module("capacity-poller")
        template = _synth(_EnabledConfig())
        env = next(iter(_capacity_lambdas(template).values()))["Properties"]["Environment"][
            "Variables"
        ]

        pools = handler._parse_instance_pools(env["INSTANCE_POOLS"])
        capacities = handler._parse_target_capacities(env["SPOT_SCORE_TARGET_CAPACITIES"])

        assert capacities == ((1, "spot_score"), (10, "spot_score_at_10"), (50, "spot_score_at_50"))
        assert pools and all(len(set(members)) >= 3 for _name, members in pools)

    def test_poller_environment_fits_the_lambda_size_limit(self):
        # Lambda rejects functions whose environment exceeds 4 KB in total.
        # The serialized pool catalog and the watch list are the two big
        # values, and both grow with the accelerator catalog, so model the
        # real deployment (full catalog watch list, several polled regions)
        # rather than this test's two-type mock and fail here instead of at
        # deploy time. The 3,800-byte bound leaves headroom for longer table
        # names under a custom project_name.
        from scripts.accelerator_catalog import Catalog

        template = _synth(_EnabledConfig())
        env = dict(
            next(iter(_capacity_lambdas(template).values()))["Properties"]["Environment"][
                "Variables"
            ]
        )
        env["WATCH_INSTANCE_TYPES"] = ",".join(Catalog.load().instance_types)
        env["ENABLED_REGIONS"] = ",".join(
            ["us-east-1", "us-east-2", "us-west-2", "eu-west-1", "eu-central-1", "ap-northeast-1"]
        )
        env["CAPACITY_HISTORY_TABLE_NAME"] = "gco-capacity-history"
        total = sum(len(key) + len(str(value)) for key, value in env.items())
        assert total < 3800, f"poller environment is {total} bytes; the Lambda limit is 4096"

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
