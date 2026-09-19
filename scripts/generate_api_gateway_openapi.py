#!/usr/bin/env python3
"""Generate the committed OpenAPI documents for the two GCO API Gateway REST APIs.

The four FastAPI services describe themselves (``scripts/generate_openapi.py``
asks each application for ``app.openapi()``). The two AWS API Gateway front
doors have no runtime to ask: they are CDK constructs. This script synthesizes
those stacks in-process — no ``cdk`` CLI, no AWS call — reads the
``AWS::ApiGateway::*`` resources out of the CloudFormation templates and writes
one OpenAPI 3.0.3 document per API:

* ``docs/openapi/api-gateway-global.json`` — ``GCOApiGatewayGlobalStack``
  (``<project>-global-api``): the edge-optimized single entry point.
* ``docs/openapi/api-gateway-regional.json`` — ``GCORegionalApiGatewayStack``
  (``<project>-regional-api-<region>``): the per-region IAM bridge.

Every operation records API Gateway's authorization as an OpenAPI security
requirement, the integration (``x-amazon-apigateway-integration``: type,
timeout, response transfer mode) and, under ``x-gco-backend``, the Lambda that
receives the request and the hops it takes from there (Global Accelerator, the
in-cluster Gateway, a regional API Gateway) — the same route table the
interaction diagram in ``diagrams/api_specs/`` is drawn from. Routes that exist
only under a deployment condition carry ``x-gco-condition``: the global stack is
synthesized three times (full, without analytics, without Global Accelerator)
and a route's condition is whichever knob makes it appear.

Usage::

    python scripts/generate_api_gateway_openapi.py           # write the documents
    python scripts/generate_api_gateway_openapi.py --check   # fail if anything is stale

``--check`` regenerates in memory and compares, so a route added to a stack
without regenerating is caught in review. Prose for a route (its tag, summary
and description) lives in ``ROUTE_DOCS`` here; a synthesized route with no entry
fails generation rather than shipping an undocumented operation.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
OUTPUT_DIR = REPO_ROOT / "docs" / "openapi"

#: Filename stems of the documents this generator owns.
DOCUMENT_NAMES: tuple[str, ...] = ("api-gateway-global", "api-gateway-regional")

OPENAPI_VERSION = "3.0.3"
#: The version of the gateway route contract, independent of the release
#: number so a version bump alone never makes these documents stale.
API_VERSION = "1.0.0"
GENERATOR = "scripts/generate_api_gateway_openapi.py"

#: CDK context key that stamps every resource with its construct path.
PATH_METADATA_CONTEXT = "aws:cdk:enable-path-metadata"
#: Construct id of the inline Lambda that stands in for the analytics stack's
#: presigned-URL function while the global stack is synthesized here.
ANALYTICS_STAND_IN_ID = "AnalyticsPresignedUrlStandIn"
#: Account of the placeholder ARNs the synthesis needs for cross-stack inputs.
#: Nothing deploys from here; ``plain`` renders it back as ``${AWS::AccountId}``.
PLACEHOLDER_ACCOUNT = "000000000000"

_HASH_SUFFIX_RE = re.compile(r"[0-9A-F]{8}$")


class ApiGatewayDocumentError(RuntimeError):
    """A synthesized API cannot be described faithfully by this generator."""


# ─── route documentation ─────────────────────────────────────────────────────


@dataclass(frozen=True)
class Hop:
    """One hop on a request's way from a proxy Lambda to the service that answers."""

    id: str
    label: str
    #: Stems of the ``docs/openapi/`` documents describing this hop, if any.
    documents: tuple[str, ...] = ()

    def as_json(self) -> dict[str, Any]:
        return {"id": self.id, "label": self.label, "documents": list(self.documents)}


@dataclass(frozen=True)
class Backend:
    """The Lambda behind an integration and the hops that follow it."""

    name: str
    source: str
    description: str
    hops: tuple[Hop, ...]


GLOBAL_ACCELERATOR = Hop(
    "global-accelerator",
    "AWS Global Accelerator: TCP/443 pass-through to the regional internal ALB",
)
CLUSTER_GATEWAY = Hop(
    "cluster-gateway",
    "Internal ALB — the Kubernetes Gateway gco-system/gco-gateway; HTTPRoute "
    "gco-routes picks the Service by longest path prefix",
    ("cluster-gateway",),
)
CONTROL_PLANE_SERVICES = Hop(
    "control-plane-services",
    "manifest-processor (catch-all and /api/v1/manifests) or health-monitor "
    "(/api/v1/health, /api/v1/metrics, /healthz)",
    ("manifest-processor", "health-monitor"),
)
INFERENCE_PROXY = Hop(
    "inference-proxy",
    "inference-proxy Service: resolves the endpoint by name, enforces its state "
    "and the serving-path allowlist",
    ("inference-proxy",),
)
INFERENCE_ENDPOINT = Hop(
    "inference-endpoint",
    "The endpoint's model-server Service in gco-inference (<name>, <name>-canary "
    "or <name>-proxy), streamed back through every hop",
)
REGIONAL_API = Hop(
    "api-gateway-regional",
    "Each region's API Gateway bridge, called with SigV4 by the aggregator's own "
    "execution role (GET/DELETE api/v1/jobs, GET api/v1/health, api/v1/status, "
    "api/v1/policy)",
    ("api-gateway-regional",),
)
REGIONAL_PROXY = Hop(
    "regional-api-proxy",
    "regional-api-proxy VPC Lambda: resolves the ALB from SSM "
    "/<project>/alb-hostname-<region> and signs the HMAC envelope",
)

#: ``(api kind, Lambda construct id) -> Backend``. A synthesized integration
#: whose Lambda is not listed here fails generation: every new front-door
#: Lambda must say where it sends requests.
BACKENDS: dict[tuple[str, str], Backend] = {
    ("global", "ApiGatewayProxyFunction"): Backend(
        name="api-gateway-proxy",
        source="lambda/api-gateway-proxy/handler.py",
        description=(
            "Buffered control-plane proxy. Fetches the HMAC signing key from Secrets "
            "Manager, rejects region-pinning headers and base64 bodies, signs the method, "
            "path, query, body digest, timestamp and nonce into the X-GCO-* envelope and "
            "forwards over private-root TLS within 28 s."
        ),
        hops=(GLOBAL_ACCELERATOR, CLUSTER_GATEWAY, CONTROL_PLANE_SERVICES),
    ),
    ("global", "InferenceStreamingProxyFunction"): Backend(
        name="inference-streaming-proxy",
        source="lambda/inference-streaming-proxy/index.mjs",
        description=(
            "Node.js response-streaming proxy (ROUTING_MODE=global). Accepts GET, HEAD "
            "and POST under /inference/<endpoint>/..., caps the body at 1 MiB, signs the "
            "HMAC envelope and streams the model server's response for up to 15 minutes."
        ),
        hops=(GLOBAL_ACCELERATOR, CLUSTER_GATEWAY, INFERENCE_PROXY, INFERENCE_ENDPOINT),
    ),
    ("global", "CrossRegionAggregatorFunction"): Backend(
        name="cross-region-aggregator",
        source="lambda/cross-region-aggregator/handler.py",
        description=(
            "Discovers every <project>-regional-api-<region> stack's RegionalApiEndpoint "
            "output, fans the request out to each regional API with SigV4 and merges the "
            "answers into one response."
        ),
        hops=(REGIONAL_API, REGIONAL_PROXY, CLUSTER_GATEWAY, CONTROL_PLANE_SERVICES),
    ),
    ("global", ANALYTICS_STAND_IN_ID): Backend(
        name="analytics-presigned-url",
        source="lambda/analytics-presigned-url/handler.py",
        description=(
            "The analytics stack's presigned-URL Lambda (GCOAnalyticsStack): exchanges the "
            "Cognito identity for a SageMaker Studio presigned login URL. Synthesized here "
            "with an inline stand-in because the analytics stack is not part of this API's "
            "template."
        ),
        hops=(),
    ),
    ("regional", "RegionalProxyFunction"): Backend(
        name="regional-api-proxy",
        source="lambda/regional-api-proxy/handler.py",
        description=(
            "Buffered VPC Lambda. Resolves the internal ALB hostname registered in SSM "
            "/<project>/alb-hostname-<region>, verifies it is an account-owned internal "
            "ALB, signs the HMAC envelope and forwards inside the VPC within 28 s."
        ),
        hops=(CLUSTER_GATEWAY, CONTROL_PLANE_SERVICES),
    ),
    ("regional", "InferenceStreamingProxyFunction"): Backend(
        name="inference-streaming-proxy",
        source="lambda/inference-streaming-proxy/index.mjs",
        description=(
            "Node.js response-streaming VPC Lambda (ROUTING_MODE=regional): the same "
            "handler as the global route, reaching the ALB directly instead of through "
            "Global Accelerator."
        ),
        hops=(CLUSTER_GATEWAY, INFERENCE_PROXY, INFERENCE_ENDPOINT),
    ),
}


@dataclass(frozen=True)
class RouteDoc:
    """Human prose for one ``(METHOD, path)`` a stack exposes."""

    tag: str
    summary: str


_CONTROL_PLANE_SUMMARIES: dict[str, str] = {
    "GET": "Read a control-plane resource",
    "HEAD": "Probe a control-plane resource",
    "POST": "Create a control-plane resource or submit a job",
    "PUT": "Replace a control-plane resource",
    "PATCH": "Update a control-plane resource",
    "DELETE": "Delete a control-plane resource",
    "OPTIONS": "Describe the methods a control-plane path accepts",
}
_INFERENCE_SUMMARIES: dict[str, str] = {
    "GET": "Read an inference endpoint's health, info or model list",
    "HEAD": "Probe an inference endpoint",
    "POST": "Generate: forward a completion request and stream the response",
}

#: ``(METHOD, path) -> RouteDoc`` for every route either stack exposes.
ROUTE_DOCS: dict[tuple[str, str], RouteDoc] = {
    **{
        (method, "/api/v1/{proxy+}"): RouteDoc("control-plane", summary)
        for method, summary in _CONTROL_PLANE_SUMMARIES.items()
    },
    **{
        (method, "/inference/{proxy+}"): RouteDoc("inference", summary)
        for method, summary in _INFERENCE_SUMMARIES.items()
    },
    ("GET", "/api/v1/global/jobs"): RouteDoc("global", "List jobs across every region"),
    ("DELETE", "/api/v1/global/jobs"): RouteDoc("global", "Bulk-delete jobs across every region"),
    ("GET", "/api/v1/global/health"): RouteDoc("global", "Health of every region's cluster"),
    ("GET", "/api/v1/global/status"): RouteDoc("global", "Aggregated status of every region"),
    ("GET", "/studio/login"): RouteDoc("studio", "Presigned SageMaker Studio login URL"),
    ("GET", "/studio/callback"): RouteDoc("studio", "OAuth redirect landing page (stub)"),
}

#: Longer description shared by every method on a path.
PATH_DESCRIPTIONS: dict[str, str] = {
    "/api/v1/{proxy+}": (
        "Catch-all for the control-plane API served by the in-cluster FastAPI services. "
        "`{proxy+}` is the rest of the path — for example `jobs`, `jobs/{namespace}/{name}`, "
        "`manifests`, `health`, `status` — so the operations available here are the "
        "`/api/v1/*` paths of the manifest-processor and health-monitor documents, minus "
        "`/api/v1/global/*`, which the aggregator serves. The request is forwarded "
        "unchanged (the stage prefix is not part of the backend path); the response is "
        "returned unchanged."
    ),
    "/inference/{proxy+}": (
        "Streaming access to deployed inference endpoints. `{proxy+}` is "
        "`<endpoint-name>` or `<endpoint-name>/<serving-path>`; the in-cluster "
        "inference-proxy resolves the endpoint by name and allows only its serving and "
        "health paths (see the inference-proxy document). Bodies are limited to 1 MiB and "
        "the connection may stay open for API Gateway's full 15-minute streaming window."
    ),
    "/api/v1/global/jobs": (
        "Fans out to `/api/v1/jobs` on every regional API Gateway and merges the "
        "results. Requires the regional bridges to be deployed; each region's answer is "
        "attributed to it in the response."
    ),
    "/api/v1/global/health": (
        "Fans out to `/api/v1/health` on every regional API Gateway and returns one "
        "health object per region."
    ),
    "/api/v1/global/status": (
        "Fans out to `/api/v1/status` on every regional API Gateway (answered by the "
        "manifest-processor) and merges templates, webhooks, resource limits and allowed "
        "namespaces per region."
    ),
    "/studio/login": (
        "Authenticated with a Cognito ID token from the Studio user pool rather than "
        "SigV4. Returns a presigned SageMaker Studio URL for the caller's user profile."
    ),
    "/studio/callback": (
        "Unauthenticated MOCK integration returning an empty 200 body: the Cognito "
        "hosted-UI OAuth redirect target. The browser redirect carries the authorization "
        "code as a query-string parameter and nothing reads the body."
    ),
}

#: Descriptions of the greedy path parameters.
PATH_PARAMETERS: dict[str, str] = {
    "proxy": "The remainder of the backend path (API Gateway greedy path variable).",
}

RESPONSE_DESCRIPTIONS: dict[str, str] = {
    "200": "Success; the backend's response is returned unchanged.",
    "400": (
        "Bad request: rejected by the proxy before forwarding (for example an "
        "`X-GCO-Target-Region` header or a base64-encoded body) or by the backend."
    ),
    "401": "Unauthorized: the Cognito ID token is missing, expired or not from the Studio pool.",
    "403": (
        "Forbidden: the caller's IAM identity or the API's resource policy denied "
        "`execute-api:Invoke`, or the backend rejected the request's HMAC envelope."
    ),
    "404": (
        "Not found: the endpoint does not exist in this region or the path is outside "
        "the inference serving-path allowlist."
    ),
    "500": "Internal error in the proxy Lambda or the backend.",
    "502": "Bad gateway: the in-cluster proxy or the model server did not answer.",
}

#: Security schemes, keyed by API Gateway's ``AuthorizationType``.
SECURITY_SCHEMES: dict[str, tuple[str, dict[str, Any]]] = {
    "AWS_IAM": (
        "sigv4",
        {
            "type": "apiKey",
            "name": "Authorization",
            "in": "header",
            "x-amazon-apigateway-authtype": "awsSigv4",
            "description": (
                "AWS Signature Version 4 for service `execute-api`, signed with IAM "
                "credentials of the deploying account that carry `execute-api:Invoke` on "
                "this API (for example `awscurl --service execute-api --region <region>`)."
            ),
        },
    ),
    "COGNITO_USER_POOLS": (
        "cognito",
        {
            "type": "apiKey",
            "name": "Authorization",
            "in": "header",
            "x-amazon-apigateway-authtype": "cognito_user_pools",
            "description": (
                "ID token issued by the Studio Cognito user pool, validated by the "
                "`<project>-studio-cognito-authorizer` authorizer."
            ),
        },
    ),
}

CONDITIONS: dict[str, dict[str, str]] = {
    "global-accelerator": {
        "id": "global-accelerator",
        "description": (
            "Present only in the commercial `aws` partition, where Global Accelerator "
            "provides the data path to the regional ALBs. Elsewhere the global API keeps "
            "only its aggregate routes and workload traffic uses each region's bridge."
        ),
    },
    "analytics": {
        "id": "analytics",
        "description": "Present only when `analytics_environment.enabled` is true in cdk.json.",
    },
}

TAGS: dict[str, str] = {
    "control-plane": "Manifests, jobs, templates, webhooks, cost and status — proxied to the cluster.",
    "global": "Cross-region aggregation, answered by the aggregator Lambda.",
    "inference": "Streaming access to deployed model endpoints.",
    "studio": "SageMaker Studio access for the analytics environment.",
}


# ─── CloudFormation template reading ─────────────────────────────────────────


def _resources_of(template: dict[str, Any], resource_type: str) -> dict[str, dict[str, Any]]:
    resources = template.get("Resources")
    if not isinstance(resources, dict):
        raise ApiGatewayDocumentError("template has no Resources")
    return {
        logical_id: resource
        for logical_id, resource in resources.items()
        if isinstance(resource, dict) and resource.get("Type") == resource_type
    }


def _properties(resource: dict[str, Any]) -> dict[str, Any]:
    properties = resource.get("Properties")
    return properties if isinstance(properties, dict) else {}


def plain(value: Any) -> Any:
    """CloudFormation intrinsics rendered as ``${...}`` placeholders, recursively.

    ``{"Ref": "AWS::AccountId"}`` becomes ``${AWS::AccountId}``, a ``Fn::GetAtt``
    becomes ``${Logical.Attribute}`` and a ``Fn::Join`` is joined — so a
    resource policy reads as the statement it deploys as, with deploy-time
    values named rather than guessed.
    """
    if isinstance(value, dict):
        if set(value) == {"Ref"}:
            return f"${{{value['Ref']}}}"
        if set(value) == {"Fn::GetAtt"}:
            target = value["Fn::GetAtt"]
            return "${" + ".".join(str(part) for part in target) + "}"
        if set(value) == {"Fn::Join"}:
            delimiter, parts = value["Fn::Join"]
            return str(delimiter).join(str(plain(part)) for part in parts)
        if set(value) == {"Fn::Sub"}:
            template = value["Fn::Sub"]
            return template if isinstance(template, str) else str(plain(template[0]))
        return {str(key): plain(item) for key, item in value.items()}
    if isinstance(value, list):
        return [plain(item) for item in value]
    if isinstance(value, str):
        return value.replace(PLACEHOLDER_ACCOUNT, "${AWS::AccountId}")
    return value


def rest_api(template: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    """The single ``AWS::ApiGateway::RestApi`` as ``(logical id, properties)``."""
    apis = _resources_of(template, "AWS::ApiGateway::RestApi")
    if len(apis) != 1:
        raise ApiGatewayDocumentError(f"expected exactly one RestApi, found {len(apis)}")
    ((logical_id, resource),) = apis.items()
    return logical_id, _properties(resource)


def resource_paths(template: dict[str, Any], api_id: str) -> dict[str, str]:
    """``{resource logical id: full path}`` by walking ``ParentId`` to the API root."""
    resources = {
        logical_id: _properties(resource)
        for logical_id, resource in _resources_of(template, "AWS::ApiGateway::Resource").items()
    }
    root = {"Fn::GetAtt": [api_id, "RootResourceId"]}
    paths: dict[str, str] = {}
    pending = dict(resources)
    while pending:
        progressed = False
        for logical_id, properties in list(pending.items()):
            parent = properties.get("ParentId")
            part = str(properties.get("PathPart", ""))
            if parent == root:
                parent_path = ""
            elif isinstance(parent, dict) and parent.get("Ref") in paths:
                parent_path = paths[str(parent["Ref"])]
            else:
                continue
            paths[logical_id] = f"{parent_path}/{part}"
            del pending[logical_id]
            progressed = True
        if not progressed:
            raise ApiGatewayDocumentError(
                f"unresolvable ParentId chain for resources {sorted(pending)}"
            )
    return paths


def lambda_construct_id(template: dict[str, Any], logical_id: str) -> str:
    """The construct id of a Lambda from its path metadata, else its logical id."""
    functions = _resources_of(template, "AWS::Lambda::Function")
    if logical_id not in functions:
        raise ApiGatewayDocumentError(f"integration target {logical_id} is not a Lambda function")
    metadata = functions[logical_id].get("Metadata")
    path = metadata.get("aws:cdk:path") if isinstance(metadata, dict) else None
    if isinstance(path, str) and path.count("/") >= 2:
        return path.split("/")[1]
    return _HASH_SUFFIX_RE.sub("", logical_id)


def integration_lambda(integration: dict[str, Any]) -> str | None:
    """The logical id of the Lambda an ``AWS_PROXY`` integration invokes."""
    uri = integration.get("Uri")
    if not isinstance(uri, dict) or "Fn::Join" not in uri:
        return None
    for part in uri["Fn::Join"][1]:
        if isinstance(part, dict) and "Fn::GetAtt" in part:
            target, attribute = part["Fn::GetAtt"]
            if attribute == "Arn":
                return str(target)
    return None


def stage_settings(template: dict[str, Any]) -> dict[str, Any]:
    """Stage name, throttling, logging, metrics and tracing of the single stage."""
    stages = _resources_of(template, "AWS::ApiGateway::Stage")
    if len(stages) != 1:
        raise ApiGatewayDocumentError(f"expected exactly one Stage, found {len(stages)}")
    (properties,) = (_properties(resource) for resource in stages.values())
    settings = [
        item
        for item in properties.get("MethodSettings", [])
        if isinstance(item, dict) and item.get("HttpMethod") == "*"
    ]
    method_settings = settings[0] if settings else {}
    return {
        "name": str(properties.get("StageName", "")),
        "throttling": {
            "rateLimit": method_settings.get("ThrottlingRateLimit"),
            "burstLimit": method_settings.get("ThrottlingBurstLimit"),
        },
        "loggingLevel": method_settings.get("LoggingLevel"),
        "dataTraceEnabled": method_settings.get("DataTraceEnabled"),
        "metricsEnabled": method_settings.get("MetricsEnabled"),
        "tracingEnabled": properties.get("TracingEnabled"),
        "accessLogging": "AccessLogSetting" in properties,
    }


def waf_rules(template: dict[str, Any]) -> list[dict[str, Any]]:
    """Name, priority and action of every rule of the WebACL, if the API has one."""
    acls = _resources_of(template, "AWS::WAFv2::WebACL")
    rules: list[dict[str, Any]] = []
    for resource in acls.values():
        for rule in _properties(resource).get("Rules", []):
            if not isinstance(rule, dict):
                continue
            statement = rule.get("Statement") or {}
            managed = statement.get("ManagedRuleGroupStatement")
            if isinstance(rule.get("Action"), dict) and rule["Action"]:
                action = next(iter(rule["Action"])).lower()
            elif isinstance(rule.get("OverrideAction"), dict) and "Count" in rule["OverrideAction"]:
                action = "count (group actions overridden)"
            else:
                # ``OverrideAction: None`` — the managed group's own rule actions apply.
                action = "group default"
            rules.append(
                {
                    "name": rule.get("Name"),
                    "priority": rule.get("Priority"),
                    "action": action,
                    "managedRuleGroup": managed.get("Name") if isinstance(managed, dict) else None,
                }
            )
    return sorted(rules, key=lambda rule: int(rule["priority"]))


def resource_policy(properties: dict[str, Any]) -> list[dict[str, Any]]:
    """The API's resource policy statements with intrinsics rendered as placeholders."""
    policy = properties.get("Policy")
    if not isinstance(policy, dict):
        return []
    statements = policy.get("Statement", [])
    return [plain(statement) for statement in statements if isinstance(statement, dict)]


# ─── OpenAPI assembly ────────────────────────────────────────────────────────


@dataclass(frozen=True)
class ApiDescription:
    """What the template cannot say about itself."""

    kind: str
    name: str
    title: str
    description: str
    stack_module: str
    construct: str
    stack_name: str
    region_default: str
    region_note: str
    endpoint_output: str
    synthesized_with: dict[str, Any]


Route = tuple[str, str]


def routes_of(template: dict[str, Any]) -> dict[Route, dict[str, Any]]:
    """``{(METHOD, path): method properties}`` for every method of the API."""
    api_id, _ = rest_api(template)
    paths = resource_paths(template, api_id)
    root = {"Fn::GetAtt": [api_id, "RootResourceId"]}
    routes: dict[Route, dict[str, Any]] = {}
    for logical_id, resource in _resources_of(template, "AWS::ApiGateway::Method").items():
        properties = _properties(resource)
        resource_ref = properties.get("ResourceId")
        if resource_ref == root:
            path = "/"
        elif isinstance(resource_ref, dict) and resource_ref.get("Ref") in paths:
            path = paths[str(resource_ref["Ref"])]
        else:
            raise ApiGatewayDocumentError(f"method {logical_id} has an unresolvable ResourceId")
        method = str(properties.get("HttpMethod", "")).upper()
        routes[(method, path)] = properties
    return routes


def _path_parameters(path: str) -> list[dict[str, Any]]:
    parameters = []
    for name in re.findall(r"\{([^}]+)\}", path):
        bare = name.rstrip("+")
        parameters.append(
            {
                "name": bare,
                "in": "path",
                "required": True,
                "schema": {"type": "string"},
                "description": PATH_PARAMETERS.get(bare, f"Path parameter `{bare}`."),
            }
        )
    return parameters


def _operation_id(method: str, path: str) -> str:
    words = [word for word in re.split(r"[^A-Za-z0-9]+", path) if word]
    camel = "".join(word[:1].upper() + word[1:] for word in words)
    return f"{method.lower()}{camel}" if camel else f"{method.lower()}Root"


def _integration(
    template: dict[str, Any], kind: str, properties: dict[str, Any]
) -> tuple[dict[str, Any], dict[str, Any]]:
    """``(x-amazon-apigateway-integration, x-gco-backend)`` for one method."""
    integration = properties.get("Integration")
    if not isinstance(integration, dict):
        raise ApiGatewayDocumentError("method has no Integration")
    extension: dict[str, Any] = {
        "type": str(integration.get("Type", "")).lower(),
    }
    if "IntegrationHttpMethod" in integration:
        extension["httpMethod"] = integration["IntegrationHttpMethod"]
    if "TimeoutInMillis" in integration:
        extension["timeoutInMillis"] = integration["TimeoutInMillis"]
    if "ResponseTransferMode" in integration:
        extension["responseTransferMode"] = integration["ResponseTransferMode"]

    if extension["type"] == "mock":
        extension["requestTemplates"] = plain(integration.get("RequestTemplates", {}))
        extension["responses"] = plain(integration.get("IntegrationResponses", []))
        backend: dict[str, Any] = {
            "integration": "mock",
            "description": "API Gateway answers directly; no Lambda and no backend is involved.",
            "hops": [],
        }
        return extension, backend

    target = integration_lambda(integration)
    if target is None:
        raise ApiGatewayDocumentError(f"{extension['type']} integration has no Lambda target")
    construct_id = lambda_construct_id(template, target)
    known = BACKENDS.get((kind, construct_id))
    if known is None:
        raise ApiGatewayDocumentError(
            f"no BACKENDS entry for ({kind!r}, {construct_id!r}); document where this "
            "Lambda sends requests in scripts/generate_api_gateway_openapi.py"
        )
    function = _properties(_resources_of(template, "AWS::Lambda::Function")[target])
    backend = {
        "integration": "lambda",
        "lambda": {
            "name": known.name,
            "source": known.source,
            "runtime": function.get("Runtime"),
            "handler": function.get("Handler"),
            "timeoutSeconds": function.get("Timeout"),
            "memorySizeMb": function.get("MemorySize"),
        },
        "description": known.description,
        "hops": [hop.as_json() for hop in known.hops],
    }
    return extension, backend


def _security(properties: dict[str, Any], schemes: dict[str, dict[str, Any]]) -> list[Any]:
    authorization = str(properties.get("AuthorizationType", "NONE")).upper()
    if authorization == "NONE":
        return []
    if authorization not in SECURITY_SCHEMES:
        raise ApiGatewayDocumentError(f"unsupported AuthorizationType {authorization!r}")
    name, scheme = SECURITY_SCHEMES[authorization]
    schemes[name] = scheme
    return [{name: []}]


def _responses(properties: dict[str, Any], mock: bool) -> dict[str, Any]:
    responses: dict[str, Any] = {}
    for response in properties.get("MethodResponses", []):
        if not isinstance(response, dict):
            continue
        status = str(response.get("StatusCode", ""))
        description = RESPONSE_DESCRIPTIONS.get(status, f"{status} response")
        if mock and status == "200":
            description = "Success; empty JSON body."
        responses[status] = {"description": description}
    if not responses:
        raise ApiGatewayDocumentError("method declares no MethodResponses")
    return responses


def build_document(
    template: dict[str, Any],
    api: ApiDescription,
    *,
    conditions: dict[Route, str] | None = None,
    direct_access_template: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """One OpenAPI document for the API a template deploys.

    ``direct_access_template`` is the same API synthesized with
    ``api_gateway.regional_api_enabled``; the resource-policy statements it adds
    are reported separately so the opt-in is visible without a second document.
    """
    _api_id, api_properties = rest_api(template)
    # Deploy-time references (the API id, the user pool ARN) say nothing about
    # the contract, so only the settings that describe behaviour are kept.
    validators = {
        logical_id: {
            key: value
            for key, value in plain(_properties(resource)).items()
            if key in {"Name", "ValidateRequestBody", "ValidateRequestParameters"}
        }
        for logical_id, resource in _resources_of(
            template, "AWS::ApiGateway::RequestValidator"
        ).items()
    }
    authorizers = {
        logical_id: {
            key: value
            for key, value in plain(_properties(resource)).items()
            if key in {"Name", "Type", "IdentitySource", "AuthorizerResultTtlInSeconds"}
        }
        for logical_id, resource in _resources_of(template, "AWS::ApiGateway::Authorizer").items()
    }
    schemes: dict[str, dict[str, Any]] = {}
    paths: dict[str, dict[str, Any]] = {}
    used_tags: set[str] = set()
    documented = set(ROUTE_DOCS)
    for (method, path), properties in sorted(routes_of(template).items()):
        doc = ROUTE_DOCS.get((method, path))
        if doc is None:
            raise ApiGatewayDocumentError(
                f"no ROUTE_DOCS entry for {method} {path}; describe the route in "
                "scripts/generate_api_gateway_openapi.py"
            )
        documented.discard((method, path))
        integration, backend = _integration(template, api.kind, properties)
        operation: dict[str, Any] = {
            "operationId": _operation_id(method, path),
            "summary": doc.summary,
            "description": PATH_DESCRIPTIONS.get(path, doc.summary),
            "tags": [doc.tag],
            "security": _security(properties, schemes),
            "responses": _responses(properties, integration["type"] == "mock"),
            "x-amazon-apigateway-integration": integration,
            "x-gco-backend": backend,
        }
        used_tags.add(doc.tag)
        authorizer = properties.get("AuthorizerId")
        if isinstance(authorizer, dict) and authorizer.get("Ref") in authorizers:
            operation["x-amazon-apigateway-authorizer"] = authorizers[str(authorizer["Ref"])]
        validator = properties.get("RequestValidatorId")
        if isinstance(validator, dict) and validator.get("Ref") in validators:
            operation["x-amazon-apigateway-request-validator"] = validators[str(validator["Ref"])]
        if conditions and (method, path) in conditions:
            condition = CONDITIONS[conditions[(method, path)]]
            operation["x-gco-condition"] = condition
            operation["description"] += f"\n\n**Only when:** {condition['description']}"
        item = paths.setdefault(path, {})
        parameters = _path_parameters(path)
        if parameters:
            item["parameters"] = parameters
        item[method.lower()] = operation

    paths = dict(sorted(paths.items()))
    endpoint_types = api_properties.get("EndpointConfiguration", {}).get("Types", [])
    stage = stage_settings(template)
    server_url = "https://{api_id}.execute-api.{region}.{url_suffix}/{stage}"
    document: dict[str, Any] = {
        "openapi": OPENAPI_VERSION,
        "info": {
            "title": api.title,
            "version": API_VERSION,
            "description": (
                f"{api_properties.get('Description', '')}\n\n{api.description}".strip()
            ),
        },
        "servers": [
            {
                "url": server_url,
                "description": (
                    f"Stage `{stage['name']}` of the deployed REST API. The full URL is the "
                    f"`{api.endpoint_output}` output of the `{api.stack_name}` stack."
                ),
                "variables": {
                    "api_id": {
                        "default": "<api-id>",
                        "description": "REST API id assigned at deploy time.",
                    },
                    "region": {"default": api.region_default, "description": api.region_note},
                    "url_suffix": {
                        "default": "amazonaws.com",
                        "description": "The partition's DNS suffix (`${AWS::URLSuffix}`).",
                    },
                    "stage": {"default": stage["name"], "enum": [stage["name"]]},
                },
            }
        ],
        "tags": [{"name": tag, "description": TAGS[tag]} for tag in sorted(used_tags)],
        "paths": paths,
        "components": {"securitySchemes": dict(sorted(schemes.items()))},
        "x-gco-source": {
            "kind": "aws-api-gateway",
            "generator": GENERATOR,
            "stack": api.stack_module,
            "construct": api.construct,
            "stackName": api.stack_name,
            "restApiName": api_properties.get("Name"),
            "synthesizedWith": api.synthesized_with,
        },
        "x-gco-endpoint-type": endpoint_types[0] if endpoint_types else None,
        "x-gco-stage": stage,
        "x-gco-resource-policy": resource_policy(api_properties),
    }
    rules = waf_rules(template)
    if rules:
        document["x-gco-waf"] = rules
    if direct_access_template is not None:
        _, direct_properties = rest_api(direct_access_template)
        baseline = resource_policy(api_properties)
        document["x-gco-resource-policy-direct-access"] = [
            statement
            for statement in resource_policy(direct_properties)
            if statement not in baseline
        ]
        if routes_of(direct_access_template).keys() != routes_of(template).keys():
            raise ApiGatewayDocumentError(
                "regional_api_enabled changed the route set; document it as a condition"
            )
    if conditions:
        document["x-gco-conditions"] = [CONDITIONS[key] for key in sorted(set(conditions.values()))]
    return document


def conditional_routes(
    full: dict[str, Any], without_analytics: dict[str, Any], minimal: dict[str, Any]
) -> dict[Route, str]:
    """Which condition each route of the full synthesis depends on.

    ``minimal`` has neither Global Accelerator nor analytics, ``without_analytics``
    has Global Accelerator only, ``full`` has both; the variants must nest.
    """
    routes_full = set(routes_of(full))
    routes_ga = set(routes_of(without_analytics))
    routes_min = set(routes_of(minimal))
    if not (routes_min <= routes_ga <= routes_full):
        raise ApiGatewayDocumentError(
            "the global API's synthesis variants do not nest: "
            f"minimal={sorted(routes_min)} ga={sorted(routes_ga)} full={sorted(routes_full)}"
        )
    conditions = dict.fromkeys(routes_ga - routes_min, "global-accelerator")
    conditions.update(dict.fromkeys(routes_full - routes_ga, "analytics"))
    return conditions


# ─── synthesis ───────────────────────────────────────────────────────────────


def cdk_context(project_root: Path, **overrides: Any) -> dict[str, Any]:
    """cdk.json's context plus path metadata and any one-level overrides."""
    cdk_json = json.loads((project_root / "cdk.json").read_text(encoding="utf-8"))
    context: dict[str, Any] = dict(cdk_json.get("context", {}))
    context[PATH_METADATA_CONTEXT] = True
    for key, value in overrides.items():
        current = context.get(key)
        if isinstance(value, dict) and isinstance(current, dict):
            context[key] = {**current, **value}
        else:
            context[key] = value
    return context


def ensure_importable(project_root: Path) -> None:
    """Put the checkout first on ``sys.path`` (a script's own directory is what Python adds)."""
    if str(project_root) not in sys.path:
        sys.path.insert(0, str(project_root))


def synthesize(project_root: Path) -> dict[str, dict[str, Any]]:
    """Templates of every variant this generator documents, from one in-process synth.

    Returns ``full``, ``without-analytics`` and ``minimal`` for the global stack
    and ``regional`` for the regional bridge. Imported lazily: the CDK (and the
    Node runtime jsii needs) is only required to regenerate, never to read.
    """
    ensure_importable(project_root)
    import aws_cdk as cdk
    from aws_cdk import assertions
    from aws_cdk import aws_ec2 as ec2
    from aws_cdk import aws_lambda as lambda_

    from cli.stacks import cdk_asset_consumer
    from gco.config.config_loader import ConfigLoader
    from gco.stacks.api_gateway_global_stack import AnalyticsApiConfig, GCOApiGatewayGlobalStack
    from gco.stacks.constants import cross_region_aggregator_role_name
    from gco.stacks.regional_api_gateway_stack import GCORegionalApiGatewayStack

    with cdk_asset_consumer(project_root):
        app = cdk.App(context=cdk_context(project_root))
        config = ConfigLoader(app)
        project = config.get_project_name()
        regions = config.get_deployment_regions()
        global_env = cdk.Environment(region=regions["api_gateway"])

        def global_stack(name: str, *, accelerator: bool) -> GCOApiGatewayGlobalStack:
            return GCOApiGatewayGlobalStack(
                app,
                name,
                global_accelerator_dns="placeholder.awsglobalaccelerator.com"
                if accelerator
                else None,
                project_name=project,
                api_gateway_config=config.get_api_gateway_config(),
                registry_region=config.get_global_region(),
                certificate_regions=regions["regional"],
                backend_tls_config=config.get_backend_tls_config(),
                env=global_env,
            )

        full = global_stack(f"{project}-api-gateway", accelerator=True)
        stand_in = lambda_.Function(
            full,
            ANALYTICS_STAND_IN_ID,
            runtime=lambda_.Runtime.PYTHON_3_13,
            handler="index.handler",
            code=lambda_.Code.from_inline("def handler(event, context):\n    return {}\n"),
        )
        full.set_analytics_config(
            AnalyticsApiConfig(
                # Parsed by ``UserPool.from_user_pool_arn``, so it must be a
                # well-formed ARN; it is never written to the document.
                user_pool_arn=(
                    f"arn:aws:cognito-idp:{regions['api_gateway']}:{PLACEHOLDER_ACCOUNT}"
                    ":userpool/studio-user-pool"
                ),
                user_pool_client_id="<studio-user-pool-client>",
                presigned_url_lambda=stand_in,
                studio_domain_name=f"{project}-analytics",
                callback_url="https://<api-id>.execute-api.<region>.amazonaws.com/prod/studio/callback",
            )
        )
        without_analytics = global_stack("variant-without-analytics", accelerator=True)
        minimal = global_stack("variant-minimal", accelerator=False)

        region = regions["regional"][0]
        regional_env = cdk.Environment(region=region)
        network = cdk.Stack(app, "variant-network", env=regional_env)
        vpc = ec2.Vpc(network, "Vpc", max_azs=2, nat_gateways=1)
        regional = GCORegionalApiGatewayStack(
            app,
            f"{project}-regional-api-{region}",
            config=config,
            region=region,
            vpc=vpc,
            # Well-formed placeholders keep the CDK's template validation quiet;
            # ``plain`` turns the placeholder account back into a named value.
            auth_secret_arn=(
                f"arn:aws:secretsmanager:{region}:{PLACEHOLDER_ACCOUNT}"
                f":secret:{project}-api-gateway-auth"
            ),
            aggregator_role_arn=(
                f"arn:aws:iam::{PLACEHOLDER_ACCOUNT}:role/"
                f"{cross_region_aggregator_role_name(project)}"
            ),
            env=regional_env,
        )
        templates = {
            name: dict(assertions.Template.from_stack(stack).to_json())
            for name, stack in (
                ("full", full),
                ("without-analytics", without_analytics),
                ("minimal", minimal),
                ("regional", regional),
            )
        }

        # The bridge's resource policy is the one thing ``regional_api_enabled``
        # changes; a second app carries that context so the document can show
        # the statements the opt-in adds.
        direct_app = cdk.App(
            context=cdk_context(project_root, api_gateway={"regional_api_enabled": True})
        )
        direct_config = ConfigLoader(direct_app)
        direct_network = cdk.Stack(direct_app, "variant-network", env=regional_env)
        direct_vpc = ec2.Vpc(direct_network, "Vpc", max_azs=2, nat_gateways=1)
        direct = GCORegionalApiGatewayStack(
            direct_app,
            f"{project}-regional-api-{region}",
            config=direct_config,
            region=region,
            vpc=direct_vpc,
            auth_secret_arn=(
                f"arn:aws:secretsmanager:{region}:{PLACEHOLDER_ACCOUNT}"
                f":secret:{project}-api-gateway-auth"
            ),
            aggregator_role_arn=(
                f"arn:aws:iam::{PLACEHOLDER_ACCOUNT}:role/"
                f"{cross_region_aggregator_role_name(project)}"
            ),
            env=regional_env,
        )
        templates["regional-direct-access"] = dict(assertions.Template.from_stack(direct).to_json())
    templates["_config"] = {
        "project": project,
        "regions": regions,
        "partition": config.get_deployment_partition(),
    }
    return templates


def describe(templates: dict[str, dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """``{document name: OpenAPI document}`` from the synthesized variants."""
    config = templates["_config"]
    project = str(config["project"])
    regions = config["regions"]
    region = str(regions["regional"][0])
    global_api = ApiDescription(
        kind="global",
        name="api-gateway-global",
        title="GCO Global API Gateway",
        description=(
            "The single authenticated entry point for GCO. Every request is signed with "
            "AWS SigV4 and authorized by IAM; the Lambda proxies behind the routes add the "
            "request-bound HMAC envelope that the in-cluster services require, so this API "
            "is the only supported caller of the cluster besides the regional bridges. "
            "`/api/v1/{proxy+}` and `/inference/{proxy+}` reach the regions over Global "
            "Accelerator; `/api/v1/global/*` fans out to every regional API Gateway; "
            "`/studio/*` exists only with the analytics environment."
        ),
        stack_module="gco/stacks/api_gateway_global_stack.py",
        construct="GCOApiGatewayGlobalStack",
        stack_name=f"{project}-api-gateway",
        region_default=str(regions["api_gateway"]),
        region_note="`deployment_regions.api_gateway` in cdk.json.",
        endpoint_output="ApiEndpoint",
        synthesized_with={
            "partition": "aws",
            "globalAccelerator": True,
            "analyticsEnvironmentEnabled": True,
        },
    )
    regional_api = ApiDescription(
        kind="regional",
        name="api-gateway-regional",
        title="GCO Regional API Gateway",
        description=(
            "One IAM-authenticated REST API per deployment region, fronting that region's "
            "internal ALB through VPC Lambdas. Its first job is to be the aggregator's path "
            "into the region: the resource policy always admits the cross-region "
            "aggregator's execution role for the aggregate routes. Direct calls by other "
            "principals of the deploying account are an explicit opt-in "
            "(`api_gateway.regional_api_enabled`) in the commercial `aws` partition and "
            "the required ingress in partitions without Global Accelerator. The routes are "
            "the same control-plane and inference catch-alls as the global API, with the "
            "additional `HEAD` and `OPTIONS` methods on `/api/v1/{proxy+}`."
        ),
        stack_module="gco/stacks/regional_api_gateway_stack.py",
        construct="GCORegionalApiGatewayStack",
        stack_name=f"{project}-regional-api-{region}",
        region_default=region,
        region_note="One API per entry of `deployment_regions.regional` in cdk.json.",
        endpoint_output="RegionalApiEndpoint",
        synthesized_with={
            "partition": str(config["partition"]),
            "region": region,
            "regionalApiEnabled": False,
        },
    )
    conditions = conditional_routes(
        templates["full"], templates["without-analytics"], templates["minimal"]
    )
    return {
        global_api.name: build_document(templates["full"], global_api, conditions=conditions),
        regional_api.name: build_document(
            templates["regional"],
            regional_api,
            direct_access_template=templates["regional-direct-access"],
        ),
    }


def render(document: dict[str, Any]) -> str:
    """Serialize deterministically so regeneration produces a stable diff."""
    return json.dumps(document, indent=2, sort_keys=True) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--check",
        action="store_true",
        help="Do not write; exit non-zero if any committed document is stale.",
    )
    args = parser.parse_args(argv)

    try:
        documents = describe(synthesize(REPO_ROOT))
    except ApiGatewayDocumentError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    if tuple(sorted(documents)) != tuple(sorted(DOCUMENT_NAMES)):
        raise AssertionError("DOCUMENT_NAMES is out of step with describe()")

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    stale: list[str] = []
    for name, document in sorted(documents.items()):
        rendered = render(document)
        target = OUTPUT_DIR / f"{name}.json"
        current = target.read_text(encoding="utf-8") if target.is_file() else None
        if args.check:
            if current != rendered:
                stale.append(name)
                print(
                    f"{target.relative_to(REPO_ROOT)}: {'missing' if current is None else 'stale'}"
                )
            continue
        if current == rendered:
            print(f"{target.relative_to(REPO_ROOT)}: unchanged")
        else:
            target.write_text(rendered, encoding="utf-8")
            print(f"{target.relative_to(REPO_ROOT)}: written")

    if stale:
        print(f"\nRegenerate with: python {GENERATOR}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
