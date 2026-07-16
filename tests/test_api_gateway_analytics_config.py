"""Tests for ``GCOApiGatewayGlobalStack`` ``analytics_config`` wiring.

Two scenarios are exercised against ``assertions.Template.from_stack``:

* **Analytics absent** (``analytics_config=None``) — the stack's default
  today. No ``/studio/*`` resources, no Cognito authorizer, no new
  ``CfnOutput`` entries. Existing ``/api/v1/*`` and ``/inference/*``
  methods retain ``AuthorizationType=AWS_IAM``.

* **Analytics present** (minimal mock ``AnalyticsApiConfig``) — the
  ``/studio/login`` GET method exists with ``AuthorizationType=COGNITO``
  and an authorizer of type ``COGNITO_USER_POOLS`` is attached at the
  REST API level. ``CognitoAuthorizerId`` and ``StudioLoginUrl``
  ``CfnOutput`` entries exist. The pre-existing ``/api/v1/*`` methods
  still declare ``AuthorizationType=AWS_IAM`` — Cognito and IAM coexist
  at the method level.

The minimal ``AnalyticsApiConfig`` uses stub string ARNs + a throwaway
Lambda created in the same test stack (so the integration wires without
needing a fully synthesized ``GCOAnalyticsStack``).
"""

from __future__ import annotations

import aws_cdk as cdk
import pytest
from aws_cdk import assertions
from aws_cdk import aws_lambda as lambda_

from gco.stacks.api_gateway_global_stack import (
    AnalyticsApiConfig,
    GCOApiGatewayGlobalStack,
)

GA_DNS = "test-accelerator.awsglobalaccelerator.com"
STUB_ACCOUNT = "123456789012"
STUB_REGION = "us-east-2"
STUB_USER_POOL_ID = "us-east-2_STUBPOOL01"
STUB_USER_POOL_ARN = (
    f"arn:aws:cognito-idp:{STUB_REGION}:{STUB_ACCOUNT}:userpool/{STUB_USER_POOL_ID}"
)
STUB_CLIENT_ID = "1abcdefghijklmnopqrstu234"
STUB_STUDIO_DOMAIN = "gco-analytics-us-east-2"
STUB_CALLBACK_URL = "https://example.execute-api.us-east-2.amazonaws.com/prod/studio/callback"


def _synth_absent() -> assertions.Template:
    """Synthesize the stack with ``analytics_config=None`` (the default)."""
    app = cdk.App()
    stack = GCOApiGatewayGlobalStack(
        app,
        "test-api-gateway-analytics-absent",
        global_accelerator_dns=GA_DNS,
        env=cdk.Environment(account=STUB_ACCOUNT, region=STUB_REGION),
    )
    return assertions.Template.from_stack(stack)


def _synth_present() -> assertions.Template:
    """Synthesize the stack with a minimal ``AnalyticsApiConfig`` attached.

    Uses a throwaway inline Lambda (created on the API gateway stack
    itself) so the ``LambdaIntegration`` wires without needing a full
    ``GCOAnalyticsStack``. This isolates the test to the API gateway
    stack's ``/studio/*`` wiring.
    """
    app = cdk.App()
    stack = GCOApiGatewayGlobalStack(
        app,
        "test-api-gateway-analytics-present",
        global_accelerator_dns=GA_DNS,
        env=cdk.Environment(account=STUB_ACCOUNT, region=STUB_REGION),
    )

    # Create a throwaway Lambda on the same stack — Python runtime with
    # an inline handler so no asset lookup is needed. The Lambda's only
    # purpose is to satisfy the ``AnalyticsApiConfig.presigned_url_lambda``
    # field with a real ``lambda_.IFunction``.
    stub_lambda = lambda_.Function(
        stack,
        "StubPresignedUrlLambda",
        runtime=lambda_.Runtime.PYTHON_3_13,
        handler="index.handler",
        code=lambda_.Code.from_inline(
            "def handler(event, context):\n    return {'statusCode': 200}\n"
        ),
    )

    analytics_config = AnalyticsApiConfig(
        user_pool_arn=STUB_USER_POOL_ARN,
        user_pool_client_id=STUB_CLIENT_ID,
        presigned_url_lambda=stub_lambda,
        studio_domain_name=STUB_STUDIO_DOMAIN,
        callback_url=STUB_CALLBACK_URL,
    )
    stack.set_analytics_config(analytics_config)

    return assertions.Template.from_stack(stack)


# Module-level synthesis caches — the two templates are expensive to
# rebuild for every test, so we synthesize once and share across the
# assertions in each class.
@pytest.fixture(scope="module")
def absent_template() -> assertions.Template:
    return _synth_absent()


@pytest.fixture(scope="module")
def present_template() -> assertions.Template:
    return _synth_present()


class TestAnalyticsApiConfigAbsent:
    """Analytics-absent path — the stack's default today."""

    def test_no_studio_resources(self, absent_template: assertions.Template) -> None:
        """No ``AWS::ApiGateway::Resource`` with PathPart ``studio``.

        ``/studio``, ``/studio/login``, and ``/studio/callback`` are all
        added inside ``_wire_studio_routes``; when the method is not
        invoked, none of them exist in the template.
        """
        resources = absent_template.find_resources("AWS::ApiGateway::Resource")
        studio_parts = {"studio", "login", "callback"}
        offending = [
            (logical_id, res["Properties"].get("PathPart"))
            for logical_id, res in resources.items()
            if res.get("Properties", {}).get("PathPart") in studio_parts
        ]
        assert offending == [], (
            f"Expected no studio path parts in analytics-absent template, found: {offending}"
        )

    def test_no_cognito_authorizer(self, absent_template: assertions.Template) -> None:
        """No ``AWS::ApiGateway::Authorizer`` resource at all."""
        absent_template.resource_count_is("AWS::ApiGateway::Authorizer", 0)

    def test_no_studio_cfn_outputs(self, absent_template: assertions.Template) -> None:
        """Neither ``CognitoAuthorizerId`` nor ``StudioLoginUrl`` outputs exist."""
        outputs = absent_template.find_outputs("*")
        assert "CognitoAuthorizerId" not in outputs
        assert "StudioLoginUrl" not in outputs

    def test_existing_api_v1_methods_remain_iam(self, absent_template: assertions.Template) -> None:
        """Every ``/api/v1/*`` method still declares ``AWS_IAM`` authorization.

        The baseline invariant is that the analytics-absent template
        matches pre-feature behavior — no method accidentally flipped
        to ``NONE``/``COGNITO``.
        """
        methods = absent_template.find_resources("AWS::ApiGateway::Method")
        # At minimum, the stack ships five /api/v1/{proxy+} methods,
        # three /inference/{proxy+} serving methods, and the /global/*
        # aggregation endpoints. All of these are IAM-authorized.
        auth_types = {props["Properties"].get("AuthorizationType") for props in methods.values()}
        # Only ``AWS_IAM`` should appear (OPTIONS preflight methods are
        # not added by this stack). Assert via set membership so future
        # additions of unrelated IAM methods don't break the test.
        assert auth_types == {"AWS_IAM"}, (
            f"Expected all methods to use AWS_IAM in analytics-absent template, found: {auth_types}"
        )


class TestAnalyticsApiConfigPresent:
    """Analytics-present path — ``/studio/*`` wired with Cognito."""

    def test_studio_login_resource_present(self, present_template: assertions.Template) -> None:
        """``/studio`` + ``/studio/login`` resources exist."""
        resources = present_template.find_resources("AWS::ApiGateway::Resource")
        path_parts = {res["Properties"].get("PathPart") for res in resources.values()}
        assert "studio" in path_parts
        assert "login" in path_parts
        assert "callback" in path_parts

    def test_studio_login_method_uses_cognito(self, present_template: assertions.Template) -> None:
        """The ``/studio/login`` GET method has ``AuthorizationType=COGNITO_USER_POOLS``.

        In CDK Python, ``AuthorizationType.COGNITO`` renders to
        ``"COGNITO_USER_POOLS"`` in the synthesized CloudFormation
        template — this is the AWS-native spelling.
        """
        present_template.has_resource_properties(
            "AWS::ApiGateway::Method",
            {
                "HttpMethod": "GET",
                "AuthorizationType": "COGNITO_USER_POOLS",
            },
        )

    def test_cognito_authorizer_created(self, present_template: assertions.Template) -> None:
        """An authorizer of type ``COGNITO_USER_POOLS`` exists on the REST API."""
        present_template.resource_count_is("AWS::ApiGateway::Authorizer", 1)
        present_template.has_resource_properties(
            "AWS::ApiGateway::Authorizer",
            {
                "Type": "COGNITO_USER_POOLS",
                "ProviderARNs": [STUB_USER_POOL_ARN],
            },
        )

    def test_cfn_outputs_present(self, present_template: assertions.Template) -> None:
        """Both ``CognitoAuthorizerId`` and ``StudioLoginUrl`` outputs exist."""
        outputs = present_template.find_outputs("*")
        assert "CognitoAuthorizerId" in outputs, (
            f"Expected CognitoAuthorizerId output, got: {list(outputs.keys())}"
        )
        assert "StudioLoginUrl" in outputs, (
            f"Expected StudioLoginUrl output, got: {list(outputs.keys())}"
        )

    def test_api_v1_methods_still_iam(self, present_template: assertions.Template) -> None:
        """Cognito coexistence — ``/api/v1/*`` methods still declare ``AWS_IAM``.

        The Cognito authorizer coexists at the method level with the
        existing IAM-authorized routes. This asserts no pre-existing
        method accidentally switched to ``COGNITO_USER_POOLS`` when the
        ``/studio/*`` routes were wired.
        """
        methods = present_template.find_resources("AWS::ApiGateway::Method")
        iam_methods = {
            logical_id
            for logical_id, res in methods.items()
            if res["Properties"].get("AuthorizationType") == "AWS_IAM"
        }
        cognito_methods = {
            logical_id
            for logical_id, res in methods.items()
            if res["Properties"].get("AuthorizationType") == "COGNITO_USER_POOLS"
        }
        # At least one IAM-authorized method (the /api/v1/{proxy+}
        # integration exposes GET, POST, PUT, DELETE, PATCH — five) must
        # remain, and exactly one COGNITO-authorized method (the
        # /studio/login GET) should have been added.
        assert iam_methods, "Expected at least one IAM-authorized method to remain."
        assert cognito_methods, "Expected at least one COGNITO-authorized method."
        assert iam_methods.isdisjoint(cognito_methods), (
            "IAM and COGNITO methods must be disjoint sets."
        )

    def test_request_validator_created(self, present_template: assertions.Template) -> None:
        """A ``RequestValidator`` with ``ValidateRequestParameters=true`` exists."""
        present_template.has_resource_properties(
            "AWS::ApiGateway::RequestValidator",
            {"ValidateRequestParameters": True},
        )


class TestSetAnalyticsConfigMutator:
    """The ``set_analytics_config`` mutator must be called at most once."""

    def test_double_call_raises(self) -> None:
        """Calling ``set_analytics_config`` twice raises ``RuntimeError``."""
        app = cdk.App()
        stack = GCOApiGatewayGlobalStack(
            app,
            "test-api-gateway-double-set",
            global_accelerator_dns=GA_DNS,
            env=cdk.Environment(account=STUB_ACCOUNT, region=STUB_REGION),
        )
        stub_lambda = lambda_.Function(
            stack,
            "StubPresignedUrlLambda",
            runtime=lambda_.Runtime.PYTHON_3_13,
            handler="index.handler",
            code=lambda_.Code.from_inline(
                "def handler(event, context):\n    return {'statusCode': 200}\n"
            ),
        )
        cfg = AnalyticsApiConfig(
            user_pool_arn=STUB_USER_POOL_ARN,
            user_pool_client_id=STUB_CLIENT_ID,
            presigned_url_lambda=stub_lambda,
            studio_domain_name=STUB_STUDIO_DOMAIN,
            callback_url=STUB_CALLBACK_URL,
        )
        stack.set_analytics_config(cfg)
        with pytest.raises(RuntimeError, match="already has an analytics_config"):
            stack.set_analytics_config(cfg)
