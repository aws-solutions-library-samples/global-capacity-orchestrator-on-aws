"""
Tests for the CloudFormation drift-detection resources on the regional stack.

GCORegionalStack provisions a daily drift-detection loop — a KMS-encrypted
SNS topic, a Lambda that calls the CloudFormation drift APIs, and an
EventBridge rule that fires it on a rate schedule. These tests synthesize
the stack (patching DockerImageAsset and the helm-installer Lambda
builder so no Docker daemon is required) and assert against the resulting
CloudFormation template that the resources exist, are correctly wired,
and respect the `drift_detection` cdk.json context toggles.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import aws_cdk as cdk
from aws_cdk import assertions

from tests.test_regional_stack import MockConfigLoader, TestRegionalStackSynthesis


def _synth(app: cdk.App, construct_id: str) -> assertions.Template:
    """Synthesize the regional stack and return its Template.

    Mocks out DockerImageAsset and the helm-installer Lambda builder (same
    approach as tests/test_regional_stack.py) so tests do not depend on a
    running Docker daemon.
    """
    from gco.stacks.regional_stack import GCORegionalStack

    config = MockConfigLoader(app)

    with (
        patch("gco.stacks.regional_stack.ecr_assets.DockerImageAsset") as mock_docker,
        patch.object(
            GCORegionalStack,
            "_create_helm_installer_lambda",
            TestRegionalStackSynthesis._mock_helm_installer,
        ),
    ):
        mock_image = MagicMock()
        mock_image.image_uri = "123456789012.dkr.ecr.us-east-1.amazonaws.com/test:latest"
        mock_docker.return_value = mock_image

        stack = GCORegionalStack(
            app,
            construct_id,
            config=config,
            region="us-east-1",
            auth_secret_arn=(
                "arn:aws:secretsmanager:us-east-1:123456789012:secret:test-secret"  # nosec B106  # test fixture ARN
            ),
            env=cdk.Environment(account="123456789012", region="us-east-1"),
        )

        return assertions.Template.from_stack(stack)


def _find_drift_lambda(template: assertions.Template) -> tuple[str, dict]:
    """Return the (logical_id, resource) for the drift detection Lambda.

    Identified by env var ``STACK_NAME`` + ``SNS_TOPIC_ARN`` combination,
    which is unique to the drift detection function.
    """
    lambdas = template.find_resources("AWS::Lambda::Function")
    matches = []
    for logical_id, resource in lambdas.items():
        env = resource.get("Properties", {}).get("Environment", {}).get("Variables", {})
        if "STACK_NAME" in env and "SNS_TOPIC_ARN" in env:
            matches.append((logical_id, resource))
    assert len(matches) == 1, (
        f"Expected exactly one drift-detection Lambda "
        f"(env STACK_NAME + SNS_TOPIC_ARN), found {len(matches)}"
    )
    return matches[0]


def _find_drift_rule(template: assertions.Template) -> dict:
    """Return the EventBridge rule that targets the drift-detection Lambda."""
    drift_lambda_logical_id, _ = _find_drift_lambda(template)
    rules = template.find_resources("AWS::Events::Rule")
    matches = []
    for rule in rules.values():
        targets = rule.get("Properties", {}).get("Targets", []) or []
        for t in targets:
            arn = t.get("Arn")
            if (
                isinstance(arn, dict)
                and "Fn::GetAtt" in arn
                and arn["Fn::GetAtt"][0] == drift_lambda_logical_id
            ):
                matches.append(rule)
                break
    assert len(matches) == 1, (
        f"Expected exactly one EventBridge rule targeting the drift Lambda, found {len(matches)}"
    )
    return matches[0]


def _find_drift_iam_policy(template: assertions.Template) -> dict:
    """Return the IAM managed policy containing the CloudFormation drift actions."""
    policies = template.find_resources("AWS::IAM::Policy")
    required_actions = {
        "cloudformation:DetectStackDrift",
        "cloudformation:DescribeStackDriftDetectionStatus",
        "cloudformation:DescribeStackResourceDrifts",
    }
    matches = []
    for policy in policies.values():
        doc = policy.get("Properties", {}).get("PolicyDocument", {})
        for stmt in doc.get("Statement", []):
            actions = stmt.get("Action", [])
            if isinstance(actions, str):
                actions = [actions]
            if required_actions.issubset(set(actions)):
                matches.append(policy)
                break
    assert matches, (
        "No IAM policy found containing the required CloudFormation drift actions "
        f"{required_actions!r}"
    )
    return matches[0]


class TestDriftDetection:
    """Drift-detection resource assertions.

    Verifies the drift-detection Lambda and its supporting IAM / EventBridge
    resources are present in the synthesized stack so CloudFormation drift
    is monitored continuously rather than only on explicit deploys."""

    def test_drift_detection_lambda_exists(self):
        """Lambda function exists with python3.14 runtime and handler.lambda_handler."""
        template = _synth(cdk.App(), "test-drift-lambda")

        _, drift_lambda = _find_drift_lambda(template)
        props = drift_lambda["Properties"]

        assert props.get("Runtime") == "python3.14", (
            f"Expected drift Lambda runtime python3.14, got {props.get('Runtime')!r}"
        )
        assert props.get("Handler") == "handler.lambda_handler", (
            f"Expected handler 'handler.lambda_handler', got {props.get('Handler')!r}"
        )

    def test_drift_detection_sns_topic_exists(self):
        """An SNS topic with the drift-alerts display name is synthesized."""
        template = _synth(cdk.App(), "test-drift-sns")

        topics = template.find_resources("AWS::SNS::Topic")
        drift_topics = [
            t
            for t in topics.values()
            if t.get("Properties", {}).get("DisplayName") == "GCO CloudFormation Drift Alerts"
        ]
        assert len(drift_topics) == 1, (
            f"Expected exactly one drift-alerts SNS topic, found {len(drift_topics)}"
        )

        # Topic should be KMS-encrypted with a customer-managed key (KmsMasterKeyId set).
        topic_props = drift_topics[0]["Properties"]
        assert "KmsMasterKeyId" in topic_props, (
            "Drift SNS topic must be KMS-encrypted (KmsMasterKeyId property missing)"
        )

    def test_drift_detection_eventbridge_rule_is_daily(self):
        """EventBridge rule fires on a 24-hour rate schedule by default.

        CDK canonicalizes ``rate(24 hours)`` to ``rate(1 day)`` in the
        synthesized template, so both forms are accepted.
        """
        template = _synth(cdk.App(), "test-drift-rule")

        rule = _find_drift_rule(template)
        expr = rule["Properties"].get("ScheduleExpression")
        assert expr in {
            "rate(24 hours)",
            "rate(1 day)",
        }, f"Expected default daily schedule, got {expr!r}"

    def test_drift_detection_rule_targets_lambda(self):
        """EventBridge rule references the drift Lambda via Fn::GetAtt Arn."""
        template = _synth(cdk.App(), "test-drift-target")

        drift_lambda_logical_id, _ = _find_drift_lambda(template)
        rule = _find_drift_rule(template)
        targets = rule["Properties"]["Targets"]
        target_arns = [t["Arn"] for t in targets]

        assert any(
            isinstance(a, dict)
            and a.get("Fn::GetAtt", [None, None])[0] == drift_lambda_logical_id
            and a.get("Fn::GetAtt", [None, None])[1] == "Arn"
            for a in target_arns
        ), f"Rule targets do not reference drift Lambda ARN: {target_arns!r}"

    def test_drift_detection_lambda_has_cloudformation_permissions(self):
        """Lambda role has IAM policy for DetectStackDrift + describe APIs."""
        template = _synth(cdk.App(), "test-drift-iam")

        policy = _find_drift_iam_policy(template)
        statements = policy["Properties"]["PolicyDocument"]["Statement"]

        required = {
            "cloudformation:DetectStackDrift",
            "cloudformation:DescribeStackDriftDetectionStatus",
            "cloudformation:DescribeStackResourceDrifts",
        }
        found = set()
        for stmt in statements:
            if stmt.get("Effect") != "Allow":
                continue
            actions = stmt.get("Action", [])
            if isinstance(actions, str):
                actions = [actions]
            found.update(actions)

        missing = required - found
        assert not missing, f"Drift Lambda policy is missing actions: {missing!r}"

    def test_drift_detection_disabled_via_context(self):
        """Setting drift_detection.enabled=false skips all drift resources."""
        app = cdk.App(context={"drift_detection": {"enabled": False}})
        template = _synth(app, "test-drift-disabled")

        # No SNS topic with the drift display name.
        topics = template.find_resources("AWS::SNS::Topic")
        drift_topics = [
            t
            for t in topics.values()
            if t.get("Properties", {}).get("DisplayName") == "GCO CloudFormation Drift Alerts"
        ]
        assert drift_topics == [], (
            f"Drift SNS topic should not be created when disabled; found {len(drift_topics)}"
        )

        # No Lambda function carrying the drift-detection env vars.
        lambdas = template.find_resources("AWS::Lambda::Function")
        drift_lambdas = [
            fn
            for fn in lambdas.values()
            if {"STACK_NAME", "SNS_TOPIC_ARN"}.issubset(
                set(fn.get("Properties", {}).get("Environment", {}).get("Variables", {}).keys())
            )
        ]
        assert drift_lambdas == [], (
            f"Drift Lambda should not be created when disabled; found {len(drift_lambdas)}"
        )

        # No IAM policy containing the drift-specific CloudFormation actions.
        policies = template.find_resources("AWS::IAM::Policy")
        drift_policies = [
            p
            for p in policies.values()
            if any(
                "cloudformation:DetectStackDrift"
                in (
                    [s.get("Action")]
                    if isinstance(s.get("Action"), str)
                    else (s.get("Action") or [])
                )
                for s in p.get("Properties", {}).get("PolicyDocument", {}).get("Statement", [])
            )
        ]
        assert drift_policies == [], (
            "IAM policy with DetectStackDrift should not be created when disabled"
        )

    def test_drift_detection_custom_schedule_hours(self):
        """Custom drift_detection.schedule_hours context propagates to the rule."""
        app = cdk.App(context={"drift_detection": {"schedule_hours": 6}})
        template = _synth(app, "test-drift-custom-schedule")

        rule = _find_drift_rule(template)
        expr = rule["Properties"].get("ScheduleExpression")
        assert expr == "rate(6 hours)", (
            f"Expected schedule 'rate(6 hours)' from context override, got {expr!r}"
        )
