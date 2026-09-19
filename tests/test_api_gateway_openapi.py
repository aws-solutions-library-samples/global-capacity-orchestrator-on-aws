"""``scripts/generate_api_gateway_openapi.py`` — the API Gateway OpenAPI documents.

Two layers are pinned. The CloudFormation-to-OpenAPI conversion is exercised on
hand-built templates shaped like the ones the stacks synthesize (resources
chained through ``ParentId``, ``AWS_PROXY`` integrations whose ``Uri`` names a
Lambda by ``Fn::GetAtt``, a stage with method settings, a WebACL, a Cognito
authorizer and a request validator), including every refusal the converter
keeps for a shape it cannot describe. Then the committed documents are checked
against a fresh in-process synthesis of the real stacks, so a route added to
``GCOApiGatewayGlobalStack`` or ``GCORegionalApiGatewayStack`` without
regenerating fails here.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = PROJECT_ROOT / "scripts" / "generate_api_gateway_openapi.py"

API = "GCOGlobalApiF7577492"


@pytest.fixture(scope="module")
def gen() -> ModuleType:
    spec = importlib.util.spec_from_file_location("gco_generate_api_gateway_openapi", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


# ─── template fixtures ───────────────────────────────────────────────────────


def _function(
    logical_id: str,
    construct_id: str | None,
    *,
    runtime: str = "python3.14",
    handler: str = "handler.lambda_handler",
) -> dict[str, Any]:
    resource: dict[str, Any] = {
        "Type": "AWS::Lambda::Function",
        "Properties": {"Runtime": runtime, "Handler": handler, "Timeout": 29, "MemorySize": 256},
    }
    if construct_id is not None:
        resource["Metadata"] = {"aws:cdk:path": f"stack/{construct_id}/Resource"}
    return {logical_id: resource}


def _resource(logical_id: str, parent: str | None, part: str) -> dict[str, Any]:
    parent_ref: dict[str, Any] = (
        {"Fn::GetAtt": [API, "RootResourceId"]} if parent is None else {"Ref": parent}
    )
    return {
        logical_id: {
            "Type": "AWS::ApiGateway::Resource",
            "Properties": {"ParentId": parent_ref, "PathPart": part, "RestApiId": {"Ref": API}},
        }
    }


def _uri(lambda_id: str) -> dict[str, Any]:
    return {
        "Fn::Join": [
            "",
            [
                "arn:",
                {"Ref": "AWS::Partition"},
                ":apigateway:us-east-2:lambda:path/2015-03-31/functions/",
                {"Fn::GetAtt": [lambda_id, "Arn"]},
                "/invocations",
            ],
        ]
    }


def _method(
    logical_id: str,
    resource: str | None,
    method: str,
    *,
    lambda_id: str | None = None,
    mock: bool = False,
    auth: str = "AWS_IAM",
    statuses: tuple[str, ...] = ("200", "500"),
    authorizer: str | None = None,
    validator: str | None = None,
    stream: bool = False,
) -> dict[str, Any]:
    integration: dict[str, Any]
    if mock:
        integration = {
            "Type": "MOCK",
            "RequestTemplates": {"application/json": '{"statusCode": 200}'},
            "IntegrationResponses": [
                {"StatusCode": "200", "ResponseTemplates": {"application/json": ""}}
            ],
        }
    else:
        integration = {
            "Type": "AWS_PROXY",
            "IntegrationHttpMethod": "POST",
            "TimeoutInMillis": 900000 if stream else 29000,
            "Uri": _uri(lambda_id or "missing"),
        }
        if stream:
            integration["ResponseTransferMode"] = "STREAM"
    properties: dict[str, Any] = {
        "HttpMethod": method,
        "ResourceId": {"Ref": resource} if resource else {"Fn::GetAtt": [API, "RootResourceId"]},
        "RestApiId": {"Ref": API},
        "AuthorizationType": auth,
        "Integration": integration,
        "MethodResponses": [{"StatusCode": status} for status in statuses],
    }
    if authorizer:
        properties["AuthorizerId"] = {"Ref": authorizer}
    if validator:
        properties["RequestValidatorId"] = {"Ref": validator}
    return {logical_id: {"Type": "AWS::ApiGateway::Method", "Properties": properties}}


def _stage(*, method_settings: bool = True) -> dict[str, Any]:
    properties: dict[str, Any] = {
        "StageName": "prod",
        "RestApiId": {"Ref": API},
        "DeploymentId": {"Ref": "Deployment"},
        "TracingEnabled": True,
        "AccessLogSetting": {"DestinationArn": {"Fn::GetAtt": ["Logs", "Arn"]}, "Format": "{}"},
    }
    if method_settings:
        properties["MethodSettings"] = [
            {"HttpMethod": "OPTIONS", "ResourcePath": "/*", "LoggingLevel": "OFF"},
            {
                "HttpMethod": "*",
                "ResourcePath": "/*",
                "DataTraceEnabled": False,
                "LoggingLevel": "INFO",
                "MetricsEnabled": True,
                "ThrottlingBurstLimit": 2000,
                "ThrottlingRateLimit": 1000,
            },
        ]
    return {"Stage": {"Type": "AWS::ApiGateway::Stage", "Properties": properties}}


def _rest_api(
    *, edge: bool = True, policy: bool = True, name: str = "gco-global-api"
) -> dict[str, Any]:
    properties: dict[str, Any] = {
        "Name": name,
        "Description": "Authenticated global aggregation API for GCO",
        "EndpointConfiguration": {"Types": ["EDGE" if edge else "REGIONAL"]},
    }
    if policy:
        properties["Policy"] = {
            "Version": "2012-10-17",
            "Statement": [
                {
                    "Action": "execute-api:Invoke",
                    "Effect": "Allow",
                    "Principal": {"AWS": "*"},
                    "Resource": "execute-api:/*",
                    "Condition": {
                        "StringEquals": {"aws:PrincipalAccount": {"Ref": "AWS::AccountId"}}
                    },
                },
                "not-a-statement",
            ],
        }
    return {API: {"Type": "AWS::ApiGateway::RestApi", "Properties": properties}}


def _waf() -> dict[str, Any]:
    return {
        "WebAcl": {
            "Type": "AWS::WAFv2::WebACL",
            "Properties": {
                "Rules": [
                    {
                        "Name": "Managed",
                        "Priority": 2,
                        "OverrideAction": {"None": {}},
                        "Statement": {
                            "ManagedRuleGroupStatement": {"Name": "AWSManagedRulesCommonRuleSet"}
                        },
                    },
                    {"Name": "RateLimit", "Priority": 0, "Action": {"Block": {}}, "Statement": {}},
                    {
                        "Name": "Counted",
                        "Priority": 1,
                        "OverrideAction": {"Count": {}},
                        "Statement": {"ManagedRuleGroupStatement": {"Name": "Group"}},
                    },
                    "not-a-rule",
                ]
            },
        }
    }


def _global_template(*, accelerator: bool = True, analytics: bool = True) -> dict[str, Any]:
    """The shape ``GCOApiGatewayGlobalStack`` synthesizes, reduced to what the converter reads."""
    resources: dict[str, Any] = {}
    resources.update(_rest_api())
    resources.update(_stage())
    resources.update(_resource("Api", None, "api"))
    resources.update(_resource("V1", "Api", "v1"))
    resources.update(_resource("Global", "V1", "global"))
    resources.update(_resource("GlobalJobs", "Global", "jobs"))
    resources.update(_resource("GlobalHealth", "Global", "health"))
    resources.update(_function("Aggregator123", "CrossRegionAggregatorFunction"))
    resources.update(_method("GlobalJobsGet", "GlobalJobs", "GET", lambda_id="Aggregator123"))
    resources.update(_method("GlobalJobsDelete", "GlobalJobs", "DELETE", lambda_id="Aggregator123"))
    resources.update(_method("GlobalHealthGet", "GlobalHealth", "GET", lambda_id="Aggregator123"))
    if accelerator:
        resources.update(_resource("Proxy", "V1", "{proxy+}"))
        resources.update(_function("ProxyFn456", "ApiGatewayProxyFunction"))
        resources.update(_method("ProxyGet", "Proxy", "GET", lambda_id="ProxyFn456"))
        resources.update(_resource("Inference", None, "inference"))
        resources.update(_resource("InferenceProxy", "Inference", "{proxy+}"))
        resources.update(
            _function(
                "StreamFn789",
                "InferenceStreamingProxyFunction",
                runtime="nodejs24.x",
                handler="index.handler",
            )
        )
        resources.update(
            _method(
                "InferencePost",
                "InferenceProxy",
                "POST",
                lambda_id="StreamFn789",
                stream=True,
                statuses=("200", "502"),
            )
        )
    if analytics:
        resources.update(_resource("Studio", None, "studio"))
        resources.update(_resource("StudioLogin", "Studio", "login"))
        resources.update(_resource("StudioCallback", "Studio", "callback"))
        resources.update(
            _function(
                "StandIn000",
                "AnalyticsPresignedUrlStandIn",
                runtime="python3.13",
                handler="index.handler",
            )
        )
        resources["Authorizer"] = {
            "Type": "AWS::ApiGateway::Authorizer",
            "Properties": {
                "Name": "gco-studio-cognito-authorizer",
                "Type": "COGNITO_USER_POOLS",
                "IdentitySource": "method.request.header.Authorization",
                "ProviderARNs": ["arn:aws:cognito-idp:us-east-2:000000000000:userpool/pool"],
                "RestApiId": {"Ref": API},
            },
        }
        resources["Validator"] = {
            "Type": "AWS::ApiGateway::RequestValidator",
            "Properties": {
                "Name": "gco-studio-request-validator",
                "ValidateRequestParameters": True,
                "RestApiId": {"Ref": API},
            },
        }
        resources.update(
            _method(
                "StudioLoginGet",
                "StudioLogin",
                "GET",
                lambda_id="StandIn000",
                auth="COGNITO_USER_POOLS",
                authorizer="Authorizer",
                validator="Validator",
                statuses=("200", "401"),
            )
        )
        resources.update(
            _method(
                "StudioCallbackGet",
                "StudioCallback",
                "GET",
                mock=True,
                auth="NONE",
                statuses=("200",),
            )
        )
    resources.update(_waf())
    return {"Resources": resources}


def _regional_template(*, direct: bool = False) -> dict[str, Any]:
    resources: dict[str, Any] = {}
    resources.update(_rest_api(edge=False, policy=False, name="gco-regional-api-us-east-1"))
    statements: list[Any] = [
        {
            "Action": "execute-api:Invoke",
            "Effect": "Allow",
            "Principal": {"AWS": "arn:aws:iam::000000000000:role/gco-cross-region-aggregator"},
            "Resource": ["execute-api:/*/GET/api/v1/jobs"],
        }
    ]
    if direct:
        statements.append(
            {
                "Action": "execute-api:Invoke",
                "Effect": "Allow",
                "Principal": {"AWS": "*"},
                "Resource": "execute-api:/*",
            }
        )
    resources[API]["Properties"]["Policy"] = {"Version": "2012-10-17", "Statement": statements}
    resources.update(_stage())
    resources.update(_resource("Api", None, "api"))
    resources.update(_resource("V1", "Api", "v1"))
    resources.update(_resource("Proxy", "V1", "{proxy+}"))
    resources.update(_function("RegionalFn1", "RegionalProxyFunction"))
    for method in ("GET", "HEAD", "OPTIONS"):
        resources.update(_method(f"Proxy{method}", "Proxy", method, lambda_id="RegionalFn1"))
    return {"Resources": resources}


def _templates() -> dict[str, dict[str, Any]]:
    return {
        "full": _global_template(),
        "without-analytics": _global_template(analytics=False),
        "minimal": _global_template(accelerator=False, analytics=False),
        "regional": _regional_template(),
        "regional-direct-access": _regional_template(direct=True),
        "_config": {
            "project": "gco",
            "regions": {
                "api_gateway": "us-east-2",
                "regional": ["us-east-1"],
                "global": "us-east-2",
            },
            "partition": "aws",
        },
    }


# ─── template reading ────────────────────────────────────────────────────────


def test_plain_renders_intrinsics_as_placeholders(gen: ModuleType) -> None:
    assert gen.plain({"Ref": "AWS::AccountId"}) == "${AWS::AccountId}"
    assert gen.plain({"Fn::GetAtt": ["Fn", "Arn"]}) == "${Fn.Arn}"
    assert (
        gen.plain({"Fn::Join": [":", ["a", {"Ref": "AWS::Region"}, "b"]]}) == "a:${AWS::Region}:b"
    )
    assert gen.plain({"Fn::Sub": "x-${AWS::Region}"}) == "x-${AWS::Region}"
    assert gen.plain({"Fn::Sub": ["y-${Z}", {"Z": "1"}]}) == "y-${Z}"
    assert gen.plain({"Key": [1, {"Ref": "R"}]}) == {"Key": [1, "${R}"]}
    assert (
        gen.plain(f"arn:aws:iam::{gen.PLACEHOLDER_ACCOUNT}:role/x")
        == "arn:aws:iam::${AWS::AccountId}:role/x"
    )
    assert gen.plain(7) == 7


def test_rest_api_requires_exactly_one(gen: ModuleType) -> None:
    logical_id, properties = gen.rest_api(_global_template())
    assert logical_id == API and properties["Name"] == "gco-global-api"
    with pytest.raises(gen.ApiGatewayDocumentError, match="found 0"):
        gen.rest_api({"Resources": {}})
    doubled = _global_template()
    doubled["Resources"]["Second"] = dict(doubled["Resources"][API])
    with pytest.raises(gen.ApiGatewayDocumentError, match="found 2"):
        gen.rest_api(doubled)
    with pytest.raises(gen.ApiGatewayDocumentError, match="no Resources"):
        gen.rest_api({})


def test_resource_paths_follow_parent_ids_in_any_order(gen: ModuleType) -> None:
    template = _global_template()
    # Reverse the declaration order: children before parents must still resolve.
    template["Resources"] = dict(reversed(list(template["Resources"].items())))
    paths = gen.resource_paths(template, API)
    assert paths["Proxy"] == "/api/v1/{proxy+}"
    assert paths["GlobalHealth"] == "/api/v1/global/health"
    assert paths["StudioCallback"] == "/studio/callback"
    orphan = _global_template()
    orphan["Resources"].update(_resource("Orphan", "Nowhere", "lost"))
    with pytest.raises(
        gen.ApiGatewayDocumentError, match=r"unresolvable ParentId chain for resources \['Orphan'\]"
    ):
        gen.resource_paths(orphan, API)


def test_lambda_construct_id_prefers_path_metadata(gen: ModuleType) -> None:
    template = _global_template()
    assert gen.lambda_construct_id(template, "ProxyFn456") == "ApiGatewayProxyFunction"
    template["Resources"].update(_function("CrossRegionAggregatorFunction91A8A201", None))
    assert (
        gen.lambda_construct_id(template, "CrossRegionAggregatorFunction91A8A201")
        == "CrossRegionAggregatorFunction"
    )
    with pytest.raises(gen.ApiGatewayDocumentError, match="is not a Lambda function"):
        gen.lambda_construct_id(template, "Stage")


def test_integration_lambda_reads_the_get_att_in_the_uri(gen: ModuleType) -> None:
    assert gen.integration_lambda({"Uri": _uri("Fn1")}) == "Fn1"
    assert gen.integration_lambda({"Type": "MOCK"}) is None
    assert (
        gen.integration_lambda(
            {"Uri": {"Fn::Join": ["", ["arn:", {"Fn::GetAtt": ["Fn1", "Name"]}]]}}
        )
        is None
    )


def test_stage_settings(gen: ModuleType) -> None:
    settings = gen.stage_settings(_global_template())
    assert settings == {
        "name": "prod",
        "throttling": {"rateLimit": 1000, "burstLimit": 2000},
        "loggingLevel": "INFO",
        "dataTraceEnabled": False,
        "metricsEnabled": True,
        "tracingEnabled": True,
        "accessLogging": True,
    }
    bare = {"Resources": _stage(method_settings=False)}
    assert gen.stage_settings(bare)["throttling"] == {"rateLimit": None, "burstLimit": None}
    with pytest.raises(gen.ApiGatewayDocumentError, match="found 0"):
        gen.stage_settings({"Resources": {}})


def test_waf_rules_are_sorted_by_priority_with_their_actions(gen: ModuleType) -> None:
    assert gen.waf_rules(_global_template()) == [
        {"name": "RateLimit", "priority": 0, "action": "block", "managedRuleGroup": None},
        {
            "name": "Counted",
            "priority": 1,
            "action": "count (group actions overridden)",
            "managedRuleGroup": "Group",
        },
        {
            "name": "Managed",
            "priority": 2,
            "action": "group default",
            "managedRuleGroup": "AWSManagedRulesCommonRuleSet",
        },
    ]
    assert gen.waf_rules(_regional_template()) == []


def test_resource_policy_renders_statements_and_skips_junk(gen: ModuleType) -> None:
    _, properties = gen.rest_api(_global_template())
    assert gen.resource_policy(properties) == [
        {
            "Action": "execute-api:Invoke",
            "Effect": "Allow",
            "Principal": {"AWS": "*"},
            "Resource": "execute-api:/*",
            "Condition": {"StringEquals": {"aws:PrincipalAccount": "${AWS::AccountId}"}},
        }
    ]
    assert gen.resource_policy({"Name": "no-policy"}) == []


def test_routes_of_maps_methods_to_full_paths(gen: ModuleType) -> None:
    routes = gen.routes_of(_global_template())
    assert set(routes) == {
        ("GET", "/api/v1/global/jobs"),
        ("DELETE", "/api/v1/global/jobs"),
        ("GET", "/api/v1/global/health"),
        ("GET", "/api/v1/{proxy+}"),
        ("POST", "/inference/{proxy+}"),
        ("GET", "/studio/login"),
        ("GET", "/studio/callback"),
    }
    root = _regional_template()
    root["Resources"].update(_method("RootGet", None, "GET", lambda_id="RegionalFn1"))
    assert ("GET", "/") in gen.routes_of(root)
    broken = _regional_template()
    broken["Resources"]["ProxyGET"]["Properties"]["ResourceId"] = {"Ref": "Ghost"}
    with pytest.raises(
        gen.ApiGatewayDocumentError, match="ProxyGET has an unresolvable ResourceId"
    ):
        gen.routes_of(broken)


def test_path_parameters_and_operation_ids(gen: ModuleType) -> None:
    assert gen._path_parameters("/api/v1/{proxy+}") == [
        {
            "name": "proxy",
            "in": "path",
            "required": True,
            "schema": {"type": "string"},
            "description": gen.PATH_PARAMETERS["proxy"],
        }
    ]
    assert gen._path_parameters("/things/{id}")[0]["description"] == "Path parameter `id`."
    assert gen._path_parameters("/plain") == []
    assert gen._operation_id("GET", "/api/v1/{proxy+}") == "getApiV1Proxy"
    assert gen._operation_id("GET", "/") == "getRoot"


# ─── operations ──────────────────────────────────────────────────────────────


def test_integration_describes_lambda_and_mock_backends(gen: ModuleType) -> None:
    template = _global_template()
    stream = template["Resources"]["InferencePost"]["Properties"]
    extension, backend = gen._integration(template, "global", stream)
    assert extension == {
        "type": "aws_proxy",
        "httpMethod": "POST",
        "timeoutInMillis": 900000,
        "responseTransferMode": "STREAM",
    }
    assert backend["integration"] == "lambda"
    assert backend["lambda"] == {
        "name": "inference-streaming-proxy",
        "source": "lambda/inference-streaming-proxy/index.mjs",
        "runtime": "nodejs24.x",
        "handler": "index.handler",
        "timeoutSeconds": 29,
        "memorySizeMb": 256,
    }
    assert [hop["id"] for hop in backend["hops"]] == [
        "global-accelerator",
        "cluster-gateway",
        "inference-proxy",
        "inference-endpoint",
    ]
    assert backend["hops"][1]["documents"] == ["cluster-gateway"]

    mock = template["Resources"]["StudioCallbackGet"]["Properties"]
    extension, backend = gen._integration(template, "global", mock)
    assert extension["type"] == "mock" and extension["requestTemplates"] == {
        "application/json": '{"statusCode": 200}'
    }
    assert backend == {
        "integration": "mock",
        "description": "API Gateway answers directly; no Lambda and no backend is involved.",
        "hops": [],
    }


def test_integration_refuses_what_it_cannot_describe(gen: ModuleType) -> None:
    template = _global_template()
    with pytest.raises(gen.ApiGatewayDocumentError, match="has no Integration"):
        gen._integration(template, "global", {"HttpMethod": "GET"})
    with pytest.raises(gen.ApiGatewayDocumentError, match="has no Lambda target"):
        gen._integration(template, "global", {"Integration": {"Type": "HTTP_PROXY"}})
    with pytest.raises(
        gen.ApiGatewayDocumentError,
        match=r"no BACKENDS entry for \('regional', 'ApiGatewayProxyFunction'\)",
    ):
        gen._integration(template, "regional", template["Resources"]["ProxyGet"]["Properties"])


def test_security_and_responses(gen: ModuleType) -> None:
    schemes: dict[str, Any] = {}
    assert gen._security({"AuthorizationType": "AWS_IAM"}, schemes) == [{"sigv4": []}]
    assert gen._security({"AuthorizationType": "COGNITO_USER_POOLS"}, schemes) == [{"cognito": []}]
    assert gen._security({"AuthorizationType": "NONE"}, schemes) == []
    assert gen._security({}, schemes) == []
    assert set(schemes) == {"sigv4", "cognito"}
    with pytest.raises(gen.ApiGatewayDocumentError, match="unsupported AuthorizationType 'CUSTOM'"):
        gen._security({"AuthorizationType": "CUSTOM"}, schemes)

    responses = gen._responses(
        {"MethodResponses": [{"StatusCode": "200"}, {"StatusCode": "418"}, "junk"]}, False
    )
    assert responses["200"] == {"description": gen.RESPONSE_DESCRIPTIONS["200"]}
    assert responses["418"] == {"description": "418 response"}
    assert gen._responses({"MethodResponses": [{"StatusCode": "200"}]}, True) == {
        "200": {"description": "Success; empty JSON body."}
    }
    with pytest.raises(gen.ApiGatewayDocumentError, match="declares no MethodResponses"):
        gen._responses({"MethodResponses": []}, False)


# ─── documents ───────────────────────────────────────────────────────────────


def test_conditional_routes_are_the_difference_between_variants(gen: ModuleType) -> None:
    templates = _templates()
    conditions = gen.conditional_routes(
        templates["full"], templates["without-analytics"], templates["minimal"]
    )
    assert conditions == {
        ("GET", "/api/v1/{proxy+}"): "global-accelerator",
        ("POST", "/inference/{proxy+}"): "global-accelerator",
        ("GET", "/studio/login"): "analytics",
        ("GET", "/studio/callback"): "analytics",
    }
    with pytest.raises(gen.ApiGatewayDocumentError, match="do not nest"):
        gen.conditional_routes(templates["minimal"], templates["full"], templates["minimal"])


def test_describe_builds_both_documents_from_the_variants(gen: ModuleType) -> None:
    documents = gen.describe(_templates())
    assert set(documents) == set(gen.DOCUMENT_NAMES)

    global_doc = documents["api-gateway-global"]
    assert global_doc["openapi"] == gen.OPENAPI_VERSION
    assert global_doc["info"]["title"] == "GCO Global API Gateway"
    assert global_doc["info"]["version"] == gen.API_VERSION
    assert global_doc["info"]["description"].startswith(
        "Authenticated global aggregation API for GCO\n\n"
    )
    assert list(global_doc["paths"]) == sorted(global_doc["paths"])
    proxy = global_doc["paths"]["/api/v1/{proxy+}"]
    assert proxy["parameters"][0]["name"] == "proxy"
    get = proxy["get"]
    assert get["operationId"] == "getApiV1Proxy"
    assert get["tags"] == ["control-plane"]
    assert get["summary"] == gen.ROUTE_DOCS[("GET", "/api/v1/{proxy+}")].summary
    assert get["description"].startswith(gen.PATH_DESCRIPTIONS["/api/v1/{proxy+}"])
    assert "**Only when:**" in get["description"]
    assert get["x-gco-condition"] == gen.CONDITIONS["global-accelerator"]
    assert get["security"] == [{"sigv4": []}]
    assert get["x-gco-backend"]["lambda"]["name"] == "api-gateway-proxy"
    jobs = global_doc["paths"]["/api/v1/global/jobs"]
    assert "x-gco-condition" not in jobs["get"] and "parameters" not in jobs
    assert jobs["delete"]["summary"] == "Bulk-delete jobs across every region"
    login = global_doc["paths"]["/studio/login"]["get"]
    assert login["security"] == [{"cognito": []}]
    assert login["x-amazon-apigateway-authorizer"] == {
        "Name": "gco-studio-cognito-authorizer",
        "Type": "COGNITO_USER_POOLS",
        "IdentitySource": "method.request.header.Authorization",
    }, "deploy-time references such as ProviderARNs are dropped"
    assert login["x-amazon-apigateway-request-validator"] == {
        "Name": "gco-studio-request-validator",
        "ValidateRequestParameters": True,
    }
    callback = global_doc["paths"]["/studio/callback"]["get"]
    assert callback["security"] == [] and callback["x-gco-backend"]["integration"] == "mock"
    assert callback["responses"] == {"200": {"description": "Success; empty JSON body."}}
    assert set(global_doc["components"]["securitySchemes"]) == {"cognito", "sigv4"}
    assert [tag["name"] for tag in global_doc["tags"]] == [
        "control-plane",
        "global",
        "inference",
        "studio",
    ]
    assert (
        global_doc["servers"][0]["url"]
        == "https://{api_id}.execute-api.{region}.{url_suffix}/{stage}"
    )
    assert global_doc["servers"][0]["variables"]["region"]["default"] == "us-east-2"
    assert global_doc["servers"][0]["variables"]["stage"] == {"default": "prod", "enum": ["prod"]}
    assert global_doc["x-gco-endpoint-type"] == "EDGE"
    assert global_doc["x-gco-stage"]["name"] == "prod"
    assert global_doc["x-gco-resource-policy"][0]["Resource"] == "execute-api:/*"
    assert [rule["name"] for rule in global_doc["x-gco-waf"]] == ["RateLimit", "Counted", "Managed"]
    assert [c["id"] for c in global_doc["x-gco-conditions"]] == ["analytics", "global-accelerator"]
    assert global_doc["x-gco-source"] == {
        "kind": "aws-api-gateway",
        "generator": gen.GENERATOR,
        "stack": "gco/stacks/api_gateway_global_stack.py",
        "construct": "GCOApiGatewayGlobalStack",
        "stackName": "gco-api-gateway",
        "restApiName": "gco-global-api",
        "synthesizedWith": {
            "partition": "aws",
            "globalAccelerator": True,
            "analyticsEnvironmentEnabled": True,
        },
    }
    assert "x-gco-resource-policy-direct-access" not in global_doc

    regional = documents["api-gateway-regional"]
    assert regional["x-gco-endpoint-type"] == "REGIONAL"
    assert set(regional["paths"]) == {"/api/v1/{proxy+}"}
    assert set(regional["paths"]["/api/v1/{proxy+}"]) == {"parameters", "get", "head", "options"}
    assert (
        regional["paths"]["/api/v1/{proxy+}"]["options"]["x-gco-backend"]["lambda"]["name"]
        == "regional-api-proxy"
    )
    assert "x-gco-waf" not in regional and "x-gco-conditions" not in regional
    assert regional["x-gco-resource-policy"][0]["Principal"] == {
        "AWS": "arn:aws:iam::${AWS::AccountId}:role/gco-cross-region-aggregator"
    }
    assert regional["x-gco-resource-policy-direct-access"] == [
        {
            "Action": "execute-api:Invoke",
            "Effect": "Allow",
            "Principal": {"AWS": "*"},
            "Resource": "execute-api:/*",
        }
    ]
    assert regional["x-gco-source"]["stackName"] == "gco-regional-api-us-east-1"
    assert regional["servers"][0]["variables"]["region"]["default"] == "us-east-1"


def test_build_document_refuses_undocumented_routes_and_route_set_drift(gen: ModuleType) -> None:
    templates = _templates()
    api = gen.ApiDescription(
        kind="regional",
        name="api-gateway-regional",
        title="t",
        description="d",
        stack_module="m",
        construct="c",
        stack_name="s",
        region_default="us-east-1",
        region_note="n",
        endpoint_output="o",
        synthesized_with={},
    )
    undocumented = _regional_template()
    undocumented["Resources"].update(_resource("Extra", "V1", "extra"))
    undocumented["Resources"].update(_method("ExtraGet", "Extra", "GET", lambda_id="RegionalFn1"))
    with pytest.raises(
        gen.ApiGatewayDocumentError, match="no ROUTE_DOCS entry for GET /api/v1/extra"
    ):
        gen.build_document(undocumented, api)

    drifted = _regional_template(direct=True)
    del drifted["Resources"]["ProxyOPTIONS"]
    with pytest.raises(
        gen.ApiGatewayDocumentError, match="regional_api_enabled changed the route set"
    ):
        gen.build_document(templates["regional"], api, direct_access_template=drifted)


def test_route_docs_and_backends_cover_exactly_the_documented_surface(gen: ModuleType) -> None:
    """The prose tables are keyed by the route/Lambda sets the stacks expose today."""
    assert set(gen.PATH_DESCRIPTIONS) == {path for _method, path in gen.ROUTE_DOCS}
    assert {doc.tag for doc in gen.ROUTE_DOCS.values()} == set(gen.TAGS)
    assert {kind for kind, _ in gen.BACKENDS} == {"global", "regional"}
    for backend in gen.BACKENDS.values():
        for hop in backend.hops:
            assert hop.as_json() == {
                "id": hop.id,
                "label": hop.label,
                "documents": list(hop.documents),
            }


# ─── synthesis context and the CLI ───────────────────────────────────────────


def test_ensure_importable_adds_the_checkout_once(
    gen: ModuleType, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(sys, "path", ["/elsewhere"])
    gen.ensure_importable(tmp_path)
    gen.ensure_importable(tmp_path)
    assert sys.path == [str(tmp_path), "/elsewhere"]


def test_cdk_context_merges_overrides_onto_cdk_json(gen: ModuleType, tmp_path: Path) -> None:
    (tmp_path / "cdk.json").write_text(
        json.dumps(
            {
                "context": {
                    "project_name": "gco",
                    "api_gateway": {"log_level": "INFO", "regional_api_enabled": False},
                }
            }
        ),
        encoding="utf-8",
    )
    context = gen.cdk_context(
        tmp_path, api_gateway={"regional_api_enabled": True}, project_name="other"
    )
    assert context[gen.PATH_METADATA_CONTEXT] is True
    assert context["api_gateway"] == {"log_level": "INFO", "regional_api_enabled": True}
    assert context["project_name"] == "other"
    (tmp_path / "cdk.json").write_text("{}", encoding="utf-8")
    assert gen.cdk_context(tmp_path) == {gen.PATH_METADATA_CONTEXT: True}


def test_main_writes_checks_and_reports(
    gen: ModuleType,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(gen, "synthesize", lambda root: _templates())
    monkeypatch.setattr(gen, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(gen, "OUTPUT_DIR", tmp_path / "docs" / "openapi")

    assert gen.main([]) == 0
    out = capsys.readouterr().out
    assert "docs/openapi/api-gateway-global.json: written" in out
    assert "docs/openapi/api-gateway-regional.json: written" in out
    written = json.loads(
        (tmp_path / "docs" / "openapi" / "api-gateway-global.json").read_text(encoding="utf-8")
    )
    assert written == gen.describe(_templates())["api-gateway-global"]

    assert gen.main([]) == 0
    assert capsys.readouterr().out.count(": unchanged") == 2
    assert gen.main(["--check"]) == 0
    assert capsys.readouterr().out == ""

    target = tmp_path / "docs" / "openapi" / "api-gateway-regional.json"
    target.write_text("{}\n", encoding="utf-8")
    (tmp_path / "docs" / "openapi" / "api-gateway-global.json").unlink()
    assert gen.main(["--check"]) == 1
    captured = capsys.readouterr()
    assert "docs/openapi/api-gateway-global.json: missing" in captured.out
    assert "docs/openapi/api-gateway-regional.json: stale" in captured.out
    assert f"Regenerate with: python {gen.GENERATOR}" in captured.err
    assert target.read_text(encoding="utf-8") == "{}\n", "--check never writes"


def test_main_reports_a_description_error_as_exit_2(
    gen: ModuleType,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    def broken(root: Path) -> dict[str, Any]:
        raise gen.ApiGatewayDocumentError("boom")

    monkeypatch.setattr(gen, "synthesize", broken)
    monkeypatch.setattr(gen, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(gen, "OUTPUT_DIR", tmp_path / "out")
    assert gen.main([]) == 2
    assert "ERROR: boom" in capsys.readouterr().err

    monkeypatch.setattr(gen, "synthesize", lambda root: _templates())
    monkeypatch.setattr(gen, "DOCUMENT_NAMES", ("api-gateway-global",))
    with pytest.raises(AssertionError, match="DOCUMENT_NAMES is out of step"):
        gen.main([])


# ─── the committed documents ─────────────────────────────────────────────────


def test_committed_documents_match_a_fresh_synthesis(gen: ModuleType) -> None:
    """The gate: a route added to either API Gateway stack must regenerate its document."""
    assert gen.main(["--check"]) == 0, (
        "committed API Gateway OpenAPI documents are stale — regenerate with "
        f"`python {gen.GENERATOR}`"
    )
    for name in gen.DOCUMENT_NAMES:
        target = PROJECT_ROOT / "docs" / "openapi" / f"{name}.json"
        document = json.loads(target.read_text(encoding="utf-8"))
        assert document["x-gco-source"]["kind"] == "aws-api-gateway"
        for path, item in document["paths"].items():
            for method, operation in item.items():
                if method == "parameters":
                    continue
                assert (method.upper(), path) in gen.ROUTE_DOCS
                assert operation["x-gco-backend"]["hops"] is not None
