"""``diagrams/api_specs/generate.py`` — gateway documents and the interaction diagram.

``tests/test_api_spec_sheets.py`` pins the rendering of a FastAPI export. The
catalogue also holds the two API Gateway documents and the cluster gateway
document, which carry ``servers``, security schemes and ``x-gco-*`` blocks the
sheets render into *Servers*, *Deployment*, *Routing* and per-operation
*Backend* sections, and from which ``api-topology.svg`` is drawn. Those paths
are exercised here on synthetic documents — well-formed ones for the layout
and the links, malformed ones for every guard the renderer keeps — so the
generated files stay a function of the documents alone.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import pytest

from diagrams.api_specs import generate as sheets

MKDOCS_YML = "site_url: https://example.test/site/\nrepo_url: https://example.test/org/repo\n"


# ─── fixtures ────────────────────────────────────────────────────────────────


def _service(title: str, paths: dict[str, Any]) -> dict[str, Any]:
    return {"openapi": "3.1.0", "info": {"title": title, "version": "1"}, "paths": paths}


def _lambda_backend(name: str, hops: list[Any], **extra: Any) -> dict[str, Any]:
    backend: dict[str, Any] = {
        "integration": "lambda",
        "lambda": {
            "name": name,
            "source": f"lambda/{name}/handler.py",
            "runtime": "python3.14",
            "handler": "handler.lambda_handler",
            "timeoutSeconds": 29,
        },
        "description": f"{name} forwards the request.",
        "hops": hops,
    }
    backend.update(extra)
    return backend


def _operation(**overrides: Any) -> dict[str, Any]:
    operation: dict[str, Any] = {
        "summary": "Do it",
        "responses": {"200": {"description": "ok"}},
        "security": [{"sigv4": []}],
        "x-amazon-apigateway-integration": {
            "type": "aws_proxy",
            "httpMethod": "POST",
            "timeoutInMillis": 29000,
        },
    }
    operation.update(overrides)
    return operation


def _global_gateway() -> dict[str, Any]:
    """An edge API with a conditional catch-all, an aggregator fan-out, a stream and a mock."""
    return {
        "openapi": "3.0.3",
        "info": {"title": "Global Gateway", "version": "1.0.0", "description": "Front door."},
        "servers": [
            {
                "url": "https://{api_id}.example/{stage}",
                "description": "Stage prod.",
                "variables": {
                    "api_id": {"default": "<api-id>", "description": "Assigned at deploy time."},
                    "stage": {"default": "prod"},
                    "odd": "not-a-variable",
                },
            },
            {"url": "https://plain.example"},
            "not-a-server",
        ],
        "paths": {
            "/api/v1/{proxy+}": {
                "parameters": [{"name": "proxy", "in": "path", "required": True}],
                "get": _operation(
                    **{
                        "x-gco-backend": _lambda_backend(
                            "edge-proxy",
                            [
                                {"id": "transit", "label": "Transit hop", "documents": []},
                                {
                                    "id": "cluster-gateway",
                                    "label": "The ALB",
                                    "documents": ["cluster-gateway"],
                                },
                                "not-a-hop",
                                {
                                    "id": "control-plane",
                                    "label": "either",
                                    "documents": ["svc-a", "svc-b"],
                                },
                                {"id": "never", "label": "after the fork"},
                            ],
                        ),
                        "x-gco-condition": {"id": "flag", "description": "only with the flag"},
                    }
                ),
                "post": _operation(
                    **{"x-gco-backend": _lambda_backend("edge-proxy", [])},
                ),
            },
            "/api/v1/global/health": {
                "get": _operation(
                    **{
                        "x-gco-backend": _lambda_backend(
                            "aggregator",
                            [
                                {
                                    "id": "api-gateway-regional",
                                    "label": "fan out",
                                    "documents": ["api-gateway-regional"],
                                },
                                {"id": "regional-proxy", "label": "that region's proxy"},
                            ],
                        )
                    }
                )
            },
            "/api/v1/global/jobs": {
                "get": _operation(**{"x-gco-backend": _lambda_backend("aggregator", [])}),
            },
            "/stream/{proxy+}": {
                "post": _operation(
                    security="not-a-list",
                    **{
                        "x-amazon-apigateway-integration": {
                            "type": "aws_proxy",
                            "responseTransferMode": "STREAM",
                        },
                        "x-gco-backend": {
                            "integration": "lambda",
                            "lambda": {"name": "streamer", "source": "index.mjs"},
                            "hops": [{"id": "model", "label": ""}],
                        },
                    },
                )
            },
            "/mock": {
                "get": _operation(
                    security=[],
                    **{
                        "x-amazon-apigateway-integration": {"type": "mock"},
                        "x-gco-backend": {"integration": "mock", "description": "  "},
                    },
                )
            },
            "junk-path": "not-a-path-item",
        },
        "components": {
            "securitySchemes": {
                "sigv4": {
                    "type": "apiKey",
                    "in": "header",
                    "name": "Authorization",
                    "x-amazon-apigateway-authtype": "awsSigv4",
                    "description": "Sign it.",
                },
                "bare": {"type": "http"},
                "junk": "not-a-scheme",
            }
        },
        "x-gco-source": {"kind": "aws-api-gateway"},
        "x-gco-endpoint-type": "EDGE",
        "x-gco-stage": {
            "name": "prod",
            "throttling": {"rateLimit": 10, "burstLimit": 20},
            "loggingLevel": "INFO",
            "dataTraceEnabled": False,
            "metricsEnabled": True,
            "tracingEnabled": True,
            "accessLogging": True,
        },
        "x-gco-resource-policy": [{"Effect": "Allow", "Resource": "execute-api:/*"}],
        "x-gco-waf": [
            {"name": "RateLimit", "priority": 0, "action": "block", "managedRuleGroup": None},
            {"name": "Common", "priority": 2, "action": "group default", "managedRuleGroup": "CRS"},
            "not-a-rule",
        ],
        "x-gco-conditions": [{"id": "flag", "description": "only with the flag"}, "junk"],
    }


def _regional_gateway() -> dict[str, Any]:
    """A regional bridge: opt-in direct access, no WAF, no stage block, no endpoint type."""
    return {
        "openapi": "3.0.3",
        "info": {"title": "Regional Gateway", "version": "1.0.0"},
        "paths": {
            "/api/v1/{proxy+}": {
                "get": _operation(
                    **{
                        "x-gco-backend": _lambda_backend(
                            "regional-proxy",
                            [
                                {
                                    "id": "cluster-gateway",
                                    "label": "ALB",
                                    "documents": ["cluster-gateway"],
                                }
                            ],
                            description=None,
                        )
                    }
                )
            }
        },
        "components": {},
        "x-gco-source": {"kind": "aws-api-gateway"},
        "x-gco-resource-policy": [{"Effect": "Allow", "Principal": "aggregator"}],
        "x-gco-resource-policy-direct-access": [{"Effect": "Allow", "Principal": "*"}],
    }


def _cluster_gateway(**overrides: Any) -> dict[str, Any]:
    document: dict[str, Any] = {
        "openapi": "3.1.0",
        "info": {"title": "Cluster Gateway", "version": "1.0.0"},
        "paths": {
            "/a": {
                "get": {
                    "summary": "A",
                    "tags": ["svc-a"],
                    "security": [{"gcoHmac": []}],
                    "responses": {"200": {"description": "ok"}},
                    "x-gco-route": {
                        "rule": "/a",
                        "service": "svc-a",
                        "namespace": "gco-system",
                        "port": 443,
                    },
                }
            }
        },
        "components": {
            "securitySchemes": {"gcoHmac": {"type": "apiKey", "in": "header", "name": "x-sig"}}
        },
        "x-gco-source": {"kind": "cluster-gateway"},
        "x-gco-gateway": {
            "name": "gw",
            "namespace": "gco-system",
            "gatewayClassName": "alb",
            "controllerName": "gateway.k8s.aws/alb",
            "listener": {"name": "https", "protocol": "HTTPS", "port": 443},
            "loadBalancer": {"scheme": "internal"},
            "targetGroup": {},
            "httpRoute": "routes",
        },
        "x-gco-routes": [
            {"prefix": "/a", "service": "svc-a", "port": 443},
            {"prefix": "/a/deep", "service": "svc-a", "port": 443},
            {"prefix": "/", "service": "svc-b", "port": 443},
            "not-a-rule",
        ],
        "x-gco-unauthenticated-paths": ["/healthz"],
        "x-gco-unreachable": [
            {"path": "/x", "service": "svc-b", "rule": "/a", "deliveredTo": "svc-a"},
            "junk",
        ],
        "x-gco-not-exposed": {
            "svc-c": {"via": "svc-b", "pathPrefix": "/api/v1/c", "description": "Internal."}
        },
        "x-gco-downstream": {
            "svc-a": {"hop": "model", "target": "model pods", "description": "By name."},
            "svc-b": {"target": "gco-inference/<endpoint> without a hop id"},
        },
    }
    document.update(overrides)
    return document


def _catalogue(tmp_path: Path, documents: dict[str, dict[str, Any]]) -> Path:
    root = tmp_path / "repo"
    (root / "docs" / "openapi").mkdir(parents=True)
    (root / "diagrams" / "api_specs").mkdir(parents=True)
    (root / "mkdocs.yml").write_text(MKDOCS_YML, encoding="utf-8")
    for name, document in documents.items():
        (root / "docs" / "openapi" / f"{name}.json").write_text(
            json.dumps(document, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
    return root


def _full_catalogue(tmp_path: Path) -> tuple[Path, dict[Path, str]]:
    root = _catalogue(
        tmp_path,
        {
            "api-gateway-global": _global_gateway(),
            "api-gateway-regional": _regional_gateway(),
            "cluster-gateway": _cluster_gateway(),
            "svc-a": _service("Service A", {"/a": {"get": {"responses": {}}}}),
            "svc-b": _service("Service B", {"/": {"get": {"responses": {}}}}),
            "svc-c": _service("Service C", {"/internal": {"get": {"responses": {}}}}),
        },
    )
    return root, sheets.expected_outputs(root)


def _sheet(outputs: dict[Path, str], root: Path, name: str) -> str:
    return outputs[root / "diagrams" / "api_specs" / f"{name}.md"]


# ─── kinds and ordering ──────────────────────────────────────────────────────


def test_document_kind_defaults_to_fastapi_and_rejects_unknown_kinds() -> None:
    assert sheets.document_kind({"paths": {}}) == "fastapi"
    assert sheets.document_kind(_global_gateway()) == "aws-api-gateway"
    assert sheets.document_kind(_cluster_gateway()) == "cluster-gateway"
    with pytest.raises(sheets.SpecSheetError, match=r"unknown x-gco-source\.kind 'other'"):
        sheets.document_kind({"paths": {}, "x-gco-source": {"kind": "other"}})


def test_catalogue_orders_front_doors_first(tmp_path: Path) -> None:
    root, outputs = _full_catalogue(tmp_path)
    index = _sheet(outputs, root, "README")
    rows = [line for line in index.splitlines() if line.startswith("| [")]
    assert [row.split("|")[1].strip() for row in rows] == [
        "[`api-gateway-global`](api-gateway-global.md)",
        "[`api-gateway-regional`](api-gateway-regional.md)",
        "[`cluster-gateway`](cluster-gateway.md)",
        "[`svc-a`](svc-a.md)",
        "[`svc-b`](svc-b.md)",
        "[`svc-c`](svc-c.md)",
    ]
    assert [row.split("|")[2].strip() for row in rows] == [
        "API Gateway",
        "API Gateway",
        "Gateway",
        "Service",
        "Service",
        "Service",
    ]


# ─── gateway sheets ──────────────────────────────────────────────────────────


def test_api_gateway_sheet_renders_servers_deployment_and_security(tmp_path: Path) -> None:
    root, outputs = _full_catalogue(tmp_path)
    text = _sheet(outputs, root, "api-gateway-global")
    assert text.startswith("# Global Gateway — API spec sheet")
    assert (
        "*API Gateway `api-gateway-global` · OpenAPI 3.0.3 · API version 1.0.0 · 6 endpoints"
        in text
    )
    assert "reads out of the synthesized CDK stack" in text

    servers = text[text.index("## Servers") : text.index("## Deployment")]
    assert "- `https://{api_id}.example/{stage}`" in servers
    assert "  Stage prod." in servers
    assert '  | `api_id` | `"<api-id>"` | Assigned at deploy time. |' in servers
    assert '  | `stage` | `"prod"` | — |' in servers
    assert "  | `odd` | — | — |" in servers, "a malformed variable still gets a row"
    assert "- `https://plain.example`" in servers, "a server without description or variables"
    assert "not-a-server" not in servers

    deployment = text[text.index("## Deployment") : text.index("## Security schemes")]
    assert "- **Endpoint type:** `EDGE`" in deployment
    assert (
        "- **Stage:** `prod` — throttling 10 req/s (burst 20), execution logging `INFO`, "
        "data trace off, metrics on, X-Ray tracing on, access logs on"
    ) in deployment
    assert "  - `flag`: only with the flag" in deployment
    assert '"Resource": "execute-api:/*"' in deployment
    assert "regional_api_enabled" not in deployment, "the global API has no direct-access variant"
    assert "| 0 | `RateLimit` | block | — |" in deployment
    assert "| 2 | `Common` | group default | `CRS` |" in deployment
    assert "not-a-rule" not in deployment

    security = text[text.index("## Security schemes") : text.index("## Endpoints")]
    assert "### `sigv4`" in security
    assert "- **Type:** apiKey — header `Authorization` (awsSigv4)" in security
    assert "Sign it." in security
    assert "### `bare`" in security and "- **Type:** http\n" in security
    assert "junk" not in security


def test_api_gateway_operations_render_security_integration_and_backend(tmp_path: Path) -> None:
    root, outputs = _full_catalogue(tmp_path)
    text = _sheet(outputs, root, "api-gateway-global")

    get = text[
        text.index('<a id="op-get-api-v1-proxy"></a>') : text.index(
            '<a id="op-post-api-v1-proxy"></a>'
        )
    ]
    assert "- **Security:** `sigv4`" in get
    assert "- **Integration:** `aws_proxy`, 29 s integration timeout" in get
    assert "**Backend**" in get
    assert (
        "- **Lambda:** `edge-proxy` (`lambda/edge-proxy/handler.py`; python3.14, "
        "handler.lambda_handler, 29 s timeout)"
    ) in get
    assert "- **Behaviour:** edge-proxy forwards the request." in get
    assert "    1. Transit hop" in get
    assert "    2. The ALB — [`cluster-gateway`](cluster-gateway.md)" in get
    assert "    3. either — [`svc-a`](svc-a.md), [`svc-b`](svc-b.md)" in get
    assert "    4. after the fork" in get
    assert "not-a-hop" not in get

    stream = text[text.index('<a id="op-post-stream-proxy"></a>') : text.index("## Schemas")]
    assert "- **Security:**" not in stream, "a malformed security value is ignored"
    assert "- **Integration:** `aws_proxy`, response streaming" in stream
    assert "- **Lambda:** `streamer` (`index.mjs`; )" in stream
    assert "- **Behaviour:**" not in stream
    assert "    1. —" in stream, "a hop with an empty label still counts"

    mock = text[
        text.index('<a id="op-get-mock"></a>') : text.index('<a id="op-post-stream-proxy"></a>')
    ]
    assert "- **Security:** none (unauthenticated)" in mock
    assert "- **Integration:** `mock`" in mock
    assert "- **Integration:** `mock`\n" in mock.split("**Backend**")[1]
    assert "- **Then:**" not in mock and "- **Behaviour:**" not in mock


def test_regional_gateway_sheet_renders_only_what_it_declares(tmp_path: Path) -> None:
    root, outputs = _full_catalogue(tmp_path)
    text = _sheet(outputs, root, "api-gateway-regional")
    assert "## Servers" not in text
    deployment = text[text.index("## Deployment") : text.index("## Endpoints")]
    assert "- **Endpoint type:**" not in deployment
    assert "- **Stage:**" not in deployment
    assert "- **Conditional routes:**" not in deployment
    assert "### WAF" not in deployment
    assert "Added when `api_gateway.regional_api_enabled` is true" in deployment
    assert '"Principal": "*"' in deployment
    assert "## Security schemes" not in text, "an empty components block renders nothing"
    assert "- **Behaviour:**" not in text, "a null backend description is skipped"
    assert "This api gateway declares no component schemas." in text


def test_gateway_details_need_a_stage_or_a_policy() -> None:
    assert sheets.render_gateway_details({"x-gco-endpoint-type": "EDGE"}) == []
    stage_only = sheets.render_gateway_details({"x-gco-stage": {"name": "prod"}})
    assert stage_only[0] == "## Deployment" and "### Resource policy" not in stage_only
    assert sheets.render_servers("not-a-list") == [] and sheets.render_servers([]) == []
    assert sheets.render_security_schemes({}) == []


# ─── the cluster gateway sheet ───────────────────────────────────────────────


def test_cluster_gateway_sheet_renders_the_routing_table(tmp_path: Path) -> None:
    root, outputs = _full_catalogue(tmp_path)
    text = _sheet(outputs, root, "cluster-gateway")
    assert "*Gateway `cluster-gateway` · OpenAPI 3.1.0" in text
    assert "composes from the HTTPRoute manifest and the service documents" in text
    routing = text[text.index("## Routing") : text.index("## Security schemes")]
    assert (
        "- **Gateway:** `gco-system/gw` (GatewayClass `alb`, controller `gateway.k8s.aws/alb`)"
        in routing
    )
    assert "- **Listener:** `https` HTTPS:443; load balancer scheme `internal`" in routing
    assert "- **HTTPRoute:** `routes`" in routing
    assert "- **Paths served without the HMAC envelope:** `/healthz`" in routing
    assert "| `/a/deep` | `svc-a` | 443 |" in routing
    assert "not-a-rule" not in routing
    assert "### Load balancer" in routing and '"scheme": "internal"' in routing
    assert "### Target group" not in routing, "an empty block renders no section"
    assert "| `/x` | `svc-b` | `/a` | `svc-a` |" in routing
    assert "junk" not in routing
    assert (
        "- [`svc-c`](svc-c.md) — reached through [`svc-b`](svc-b.md) (`/api/v1/c*`). Internal."
    ) in routing
    assert "- [`svc-a`](svc-a.md) → model pods. By name." in routing

    operation = text[text.index('<a id="op-get-a"></a>') : text.index("## Schemas")]
    assert "- **Security:** `gcoHmac`" in operation
    assert "- **Delivered by rule:** `/a` → Service `svc-a:443` in `gco-system`" in operation
    assert "This gateway declares no component schemas." in text


def test_cluster_gateway_details_are_optional_blocks() -> None:
    assert sheets.render_cluster_gateway_details({"x-gco-routes": []}) == []
    minimal = _cluster_gateway()
    for key in (
        "x-gco-unauthenticated-paths",
        "x-gco-unreachable",
        "x-gco-not-exposed",
        "x-gco-downstream",
    ):
        del minimal[key]
    minimal["x-gco-gateway"]["loadBalancer"] = "not-a-dict"
    lines = sheets.render_cluster_gateway_details(minimal)
    assert lines[0] == "## Routing"
    assert not any(line.startswith("### ") and line != "### HTTPRoute rules" for line in lines)
    assert "- **Paths served without the HMAC envelope:**" not in "\n".join(lines)


# ─── the interaction diagram ─────────────────────────────────────────────────


def test_topology_nodes_edges_and_links(tmp_path: Path) -> None:
    root, outputs = _full_catalogue(tmp_path)
    svg = outputs[root / "diagrams" / "api_specs" / sheets.TOPOLOGY_FILE]
    assert svg.startswith(
        '<?xml version="1.0" encoding="UTF-8"?>\n<svg xmlns="http://www.w3.org/2000/svg"'
    )
    assert svg.endswith("</svg>\n")
    links = set(re.findall(r'<a href="([^"]+)">', svg))
    assert links == {
        f"https://example.test/site/api/{name}/"
        for name in (
            "api-gateway-global",
            "api-gateway-regional",
            "cluster-gateway",
            "svc-a",
            "svc-b",
            "svc-c",
        )
    }
    for title in (
        "Clients",
        "Global Gateway",
        "Regional Gateway",
        "Cluster Gateway",
        "Transit",
        "Models",
    ):
        assert f">{title}<" in svg, title
    assert (
        "edge-proxy" in svg
        and "aggregator" in svg
        and "regional-proxy" in svg
        and "streamer" in svg
    )
    assert svg.count(">Models<") == 1, "the downstream hop and the API Gateway hop share one box"
    assert ">Svc Bs<" in svg, (
        "a downstream entry without a hop id is its own box, named after the service"
    )
    assert "GET POST /api/v1/{proxy+} †" in svg, "conditional routes are marked"
    assert ">/api/v1/global/*<" in svg, "sibling aggregator paths collapse to their prefix"
    assert ">SigV4 · opt-in<" in svg and ">SigV4<" in svg
    assert ">SigV4 fan-out<" in svg and 'marker-end="url(#arrow-fanout)"' in svg
    assert ">/api/v1/c*<" in svg and ">by endpoint name<" in svg
    assert ">/a/deep<" in svg and ">/a<" in svg, "one label line per prefix into a Service"
    assert 'stroke-dasharray="6 4"' in svg, "internal-only and dynamic boxes are dashed"
    assert "junk-path" not in svg and "not-a-hop" not in svg


def test_topology_is_deterministic_and_escapes_text(tmp_path: Path) -> None:
    root, outputs = _full_catalogue(tmp_path)
    first = outputs[root / "diagrams" / "api_specs" / sheets.TOPOLOGY_FILE]
    second = sheets.expected_outputs(root)[root / "diagrams" / "api_specs" / sheets.TOPOLOGY_FILE]
    assert first == second
    assert "&lt;" in first and "&gt;" in first, "angle brackets in labels are escaped"
    assert "<name>" not in first


def test_topology_with_only_services_draws_clients_and_boxes(tmp_path: Path) -> None:
    root = _catalogue(tmp_path, {"only": _service("Only", {"/": {"get": {"responses": {}}}})})
    svg = sheets.expected_outputs(root)[root / "diagrams" / "api_specs" / sheets.TOPOLOGY_FILE]
    assert ">Clients<" in svg and ">Only<" in svg
    assert 'marker-end="url(#arrow)"' not in svg, "no edges without a gateway"


def test_topology_refuses_a_hop_to_an_unknown_document(tmp_path: Path) -> None:
    gateway = _regional_gateway()
    gateway["paths"]["/api/v1/{proxy+}"]["get"]["x-gco-backend"]["hops"] = [
        {"id": "ghost", "label": "gone", "documents": ["missing-service"]}
    ]
    root = _catalogue(tmp_path, {"api-gateway-regional": gateway})
    with pytest.raises(sheets.SpecSheetError, match="unknown node 'doc:missing-service'"):
        sheets.expected_outputs(root)


def test_label_lines_collapse_only_deep_common_prefixes() -> None:
    assert sheets._label_lines(["/api/v1/global/a", "/api/v1/global/b"]) == ["/api/v1/global/*"]
    assert sheets._label_lines(["/api/v1/health", "/healthz"]) == ["/api/v1/health", "/healthz"]
    assert sheets._label_lines(["SigV4", "/x"]) == ["SigV4", "/x"]
    assert sheets._label_lines(["/only"]) == ["/only"]
    assert sheets._wrap("", 100) == [""]
