"""The facts behind the stack-wide Lambda cdk-nag acknowledgments.

``gco/stacks/nag_suppressions.add_lambda_suppressions`` acknowledges the
reserved-concurrency, dead-letter-queue, VPC, runtime and default-memory
findings for every function of every stack, so each reason has to hold for
every function family the stacks build. The earlier reasons described only
custom-resource functions ("invoked only during stack deployment", "user-facing
Lambda functions ARE placed in VPC"), while the API proxies, the aggregator and
the Studio presigned-URL function are request-path functions outside any VPC,
and two GCO functions relied on the default memory size the reason said none
did. These tests synthesize the whole app with every function family present
(analytics, the traffic dial, the vector store) and check what the reasons now
state.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import aws_cdk as cdk
import pytest
from aws_cdk import aws_lambda as lambda_

from tests._cdk_app_builder import load_context, synthesize_app

#: Path fragments of the functions aws-cdk-lib creates for custom resources:
#: provider frameworks and the AwsCustomResource singleton.
_CDK_MANAGED = ("/framework-", "/AWS679f53fac002430cb0da5b7982bd2287")


@pytest.fixture(scope="module")
def app() -> SimpleNamespace:
    """The whole app with every function family, templates read eagerly."""
    cdk_app = cdk.App(
        context=load_context(
            {
                "analytics_environment": {"enabled": True, "hyperpod": {"enabled": False}},
                "global_accelerator": {"traffic_dial": {"enabled": True, "mode": "enforce"}},
                "vector_store": {"enabled": True, "replica_regions": ["us-east-1"]},
            }
        )
    )
    assembly = synthesize_app(cdk_app)
    functions = {
        node.node.path: node
        for node in cdk_app.node.find_all()
        if isinstance(node, lambda_.CfnFunction)
    }
    return SimpleNamespace(
        functions=functions,
        templates={stack.stack_name: stack.template for stack in assembly.stacks},
    )


def _gco_functions(functions: dict[str, Any]) -> dict[str, Any]:
    return {
        path: function
        for path, function in functions.items()
        if not any(fragment in path for fragment in _CDK_MANAGED)
    }


def _resources(templates: dict[str, Any], kind: str) -> list[tuple[str, str, dict[str, Any]]]:
    return [
        (stack_name, logical_id, resource.get("Properties", {}))
        for stack_name, template in templates.items()
        for logical_id, resource in template.get("Resources", {}).items()
        if resource["Type"] == kind
    ]


def test_every_function_gco_defines_sets_its_memory_size(app: SimpleNamespace) -> None:
    """Serverless-LambdaDefaultMemorySize: only aws-cdk-lib's own functions use the default."""
    gco_functions = _gco_functions(app.functions)
    assert len(gco_functions) >= 20
    unset = sorted(path for path, function in gco_functions.items() if function.memory_size is None)
    assert unset == []


def test_the_functions_in_a_vpc_are_the_ones_the_reason_names(app: SimpleNamespace) -> None:
    """*-LambdaInsideVPC: the reason lists the functions that reach private addresses."""
    in_vpc = sorted(
        path.split("/")[-2]
        for path, function in app.functions.items()
        if function.vpc_config is not None
    )
    assert in_vpc == [
        "GaRegistrationFunction",
        "HelmInstallerFunction",
        "InferenceStreamingProxyFunction",
        "KubectlApplierFunction",
        "RegionalProxyFunction",
    ]
    vpc_stacks = {
        path.split("/")[0] for path, function in app.functions.items() if function.vpc_config
    }
    assert vpc_stacks == {"gco-us-east-1", "gco-regional-api-us-east-1"}


def test_only_the_backend_tls_manager_reserves_concurrency(app: SimpleNamespace) -> None:
    """*-LambdaConcurrency: the one reservation the reason mentions."""
    reserved = {
        path.split("/")[-2]: function.reserved_concurrent_executions
        for path, function in app.functions.items()
        if function.reserved_concurrent_executions is not None
    }
    assert reserved == {"BackendTlsCertificateManager": 1}


def test_scheduled_targets_retry_twice_and_keep_undelivered_events(app: SimpleNamespace) -> None:
    """*-LambdaDLQ: each EventBridge target of a function retries and has a dead-letter queue."""
    lambda_targets = [
        (stack_name, logical_id, target)
        for stack_name, logical_id, rule in _resources(app.templates, "AWS::Events::Rule")
        for target in rule.get("Targets", [])
        if "Fn::GetAtt" in target["Arn"]
    ]
    assert sorted(logical_id[:-8] for _, logical_id, _ in lambda_targets) == [
        "BackendTlsRotationSchedule",
        "CapacityPollerSchedule",
        "DriftDetectionSchedule",
        "TrafficDialSchedule",
    ]
    for stack_name, logical_id, target in lambda_targets:
        assert target["RetryPolicy"]["MaximumRetryAttempts"] == 2, (stack_name, logical_id)
        assert "Arn" in target["DeadLetterConfig"], (stack_name, logical_id)


def test_the_s3_invoked_vector_ingest_function_has_a_dlq(app: SimpleNamespace) -> None:
    """*-LambdaDLQ: the one function invoked asynchronously with data keeps failed events."""
    (ingest,) = [
        function
        for path, function in app.functions.items()
        if path.split("/")[-2] == "VectorIngestFunction"
    ]
    assert ingest.dead_letter_config is not None


def test_api_gateway_throttles_every_stage(app: SimpleNamespace) -> None:
    """*-LambdaConcurrency: the stage throttling bounds the request-path functions."""
    stages = _resources(app.templates, "AWS::ApiGateway::Stage")
    assert {stack_name for stack_name, _, _ in stages} == {
        "gco-api-gateway",
        "gco-regional-api-us-east-1",
    }
    for stack_name, logical_id, stage in stages:
        (settings,) = [
            setting
            for setting in stage["MethodSettings"]
            if setting["HttpMethod"] == "*" and setting["ResourcePath"] == "/*"
        ]
        assert settings["ThrottlingRateLimit"] > 0, (stack_name, logical_id)
        assert settings["ThrottlingBurstLimit"] > 0, (stack_name, logical_id)
