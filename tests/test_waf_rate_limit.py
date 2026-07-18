"""
Tests for the WAF PerIPRateLimit rule on GCOApiGatewayGlobalStack.

Synthesizes the stack and asserts the AWS::WAFv2::WebACL is present
with a PerIPRateLimit rule at priority 0 (evaluated before any
managed rule groups), aggregating by IP with a Block action and a
default limit of 100 requests per 5-minute window. Also verifies the
``waf.per_ip_rate_limit`` cdk.json context override flows through to
the synthesized rule, throttling abusive source IPs before they
reach the backend. The same synthesized WebACL assertions verify that the
managed CRS body-size rule alone is counted, while an explicit 8 KiB block is
retained for non-inference routes and `/prod/inference/*` reaches the authoritative
1 MiB backend middleware.
"""

from __future__ import annotations

import aws_cdk as cdk
from aws_cdk import assertions

from gco.stacks.api_gateway_global_stack import GCOApiGatewayGlobalStack

GA_DNS = "test-accelerator.awsglobalaccelerator.com"


def _synth(app: cdk.App, construct_id: str = "test-waf-stack") -> assertions.Template:
    """Synthesize the API Gateway global stack and return its Template."""
    stack = GCOApiGatewayGlobalStack(
        app,
        construct_id,
        global_accelerator_dns=GA_DNS,
    )
    return assertions.Template.from_stack(stack)


class TestWafRateLimitRule:
    """WAF per-IP rate-limit rule assertions.

    Verifies the synthesized WAFv2 WebACL contains a rate-based rule
    with the expected action, aggregate key, scope, and default limit,
    and that the limit is overridable via cdk.json context."""

    def test_web_acl_is_created(self):
        """The synthesized template contains an AWS::WAFv2::WebACL."""
        template = _synth(cdk.App())
        template.resource_count_is("AWS::WAFv2::WebACL", 1)

    def test_per_ip_rate_limit_rule_default(self):
        """PerIPRateLimit rule exists with priority 0, default limit 100, IP aggregation, Block action."""
        template = _synth(cdk.App())

        template.has_resource_properties(
            "AWS::WAFv2::WebACL",
            {
                "Rules": assertions.Match.array_with(
                    [
                        assertions.Match.object_like(
                            {
                                "Name": "PerIPRateLimit",
                                "Priority": 0,
                                "Action": {"Block": {}},
                                "Statement": {
                                    "RateBasedStatement": {
                                        "Limit": 100,
                                        "AggregateKeyType": "IP",
                                    }
                                },
                            }
                        )
                    ]
                )
            },
        )

    def test_per_ip_rate_limit_is_lowest_priority_number(self):
        """PerIPRateLimit must have the lowest priority number (evaluated first).

        WAF evaluates rules in ascending priority order, so priority 0 runs before
        all managed rule groups. This ensures abusive IPs are blocked before the
        WebACL spends WCUs on heavier managed rule groups.
        """
        template = _synth(cdk.App())

        web_acls = template.find_resources("AWS::WAFv2::WebACL")
        assert len(web_acls) == 1, "Expected exactly one WebACL"

        (web_acl,) = web_acls.values()
        rules = web_acl["Properties"]["Rules"]
        assert len(rules) >= 2, "Expected multiple WAF rules (rate limit + managed rule groups)"

        per_ip = next((r for r in rules if r.get("Name") == "PerIPRateLimit"), None)
        assert per_ip is not None, "PerIPRateLimit rule not found"

        per_ip_priority = per_ip["Priority"]
        other_priorities = [r["Priority"] for r in rules if r.get("Name") != "PerIPRateLimit"]

        assert other_priorities, "Expected at least one non-rate-limit rule"
        assert per_ip_priority < min(other_priorities), (
            f"PerIPRateLimit priority ({per_ip_priority}) must be lower than all other "
            f"rule priorities ({other_priorities})"
        )
        assert per_ip_priority == 0, "PerIPRateLimit priority should be 0"

    def test_per_ip_rate_limit_custom_context_value(self):
        """waf.per_ip_rate_limit context value propagates to the synthesized rule."""
        app = cdk.App(context={"waf": {"per_ip_rate_limit": 5000}})
        template = _synth(app, construct_id="test-waf-custom-limit")

        template.has_resource_properties(
            "AWS::WAFv2::WebACL",
            {
                "Rules": assertions.Match.array_with(
                    [
                        assertions.Match.object_like(
                            {
                                "Name": "PerIPRateLimit",
                                "Priority": 0,
                                "Action": {"Block": {}},
                                "Statement": {
                                    "RateBasedStatement": {
                                        "Limit": 5000,
                                        "AggregateKeyType": "IP",
                                    }
                                },
                            }
                        )
                    ]
                )
            },
        )

    def test_crs_counts_only_managed_body_size_restriction(self):
        """Only CRS SizeRestrictions_BODY is overridden; all other CRS rules block."""
        template = _synth(cdk.App(), construct_id="test-waf-crs-body-override")
        web_acls = template.find_resources("AWS::WAFv2::WebACL")
        (web_acl,) = web_acls.values()
        rules = web_acl["Properties"]["Rules"]
        crs_rule = next(rule for rule in rules if rule["Name"] == "AWSManagedRulesCommonRuleSet")

        assert crs_rule["Priority"] == 2
        assert crs_rule["OverrideAction"] == {"None": {}}
        managed_statement = crs_rule["Statement"]["ManagedRuleGroupStatement"]
        assert managed_statement["VendorName"] == "AWS"
        assert managed_statement["Name"] == "AWSManagedRulesCommonRuleSet"
        assert managed_statement["RuleActionOverrides"] == [
            {
                "Name": "SizeRestrictions_BODY",
                "ActionToUse": {"Count": {}},
            }
        ]

    def test_non_inference_body_limit_preserves_8_kib_control_plane_cap(self):
        """Bodies over 8 KiB block outside /prod/inference/*, including WAF oversize bodies."""
        template = _synth(cdk.App(), construct_id="test-waf-non-inference-body-limit")
        web_acls = template.find_resources("AWS::WAFv2::WebACL")
        (web_acl,) = web_acls.values()
        rules = web_acl["Properties"]["Rules"]
        body_rule = next(rule for rule in rules if rule["Name"] == "NonInferenceBodySizeLimit")

        assert body_rule["Priority"] == 1
        assert body_rule["Action"] == {"Block": {}}
        assert body_rule["Statement"] == {
            "AndStatement": {
                "Statements": [
                    {
                        "SizeConstraintStatement": {
                            "ComparisonOperator": "GT",
                            "FieldToMatch": {
                                "Body": {"OversizeHandling": "MATCH"},
                            },
                            "Size": 8192,
                            "TextTransformations": [
                                {"Priority": 0, "Type": "NONE"},
                            ],
                        }
                    },
                    {
                        "NotStatement": {
                            "Statement": {
                                "ByteMatchStatement": {
                                    "FieldToMatch": {"UriPath": {}},
                                    "PositionalConstraint": "STARTS_WITH",
                                    "SearchString": "/prod/inference/",
                                    "TextTransformations": [
                                        {"Priority": 0, "Type": "NONE"},
                                    ],
                                }
                            }
                        }
                    },
                ]
            }
        }

        inference_prefix = body_rule["Statement"]["AndStatement"]["Statements"][1]["NotStatement"][
            "Statement"
        ]["ByteMatchStatement"]["SearchString"]
        assert "/prod/inference/model/v1/completions".startswith(inference_prefix)
        assert not "/prod/inference-extra/model".startswith(inference_prefix)
