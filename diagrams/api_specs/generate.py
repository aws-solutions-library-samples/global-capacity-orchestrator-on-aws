#!/usr/bin/env python3
"""Render the API spec sheets: one Markdown page per GCO HTTP service.

The source of truth is what FastAPI itself generates. ``scripts/generate_openapi.py``
asks each application for ``app.openapi()`` and commits the result under
``docs/openapi/<service>.json`` — the document behind the service's automatic
Swagger ``/docs`` page — and ``tests/test_api_docs_coverage.py`` fails when
those documents drift from the live routes. This catalogue turns each document
into two human-facing renderings:

* ``diagrams/api_specs/<service>.md`` — a Markdown spec sheet: the endpoint
  table, every operation's parameters, request body and responses, and every
  component schema as a property table. It renders on GitHub and, injected by
  ``scripts/mkdocs_hooks.py``, as a page of the MkDocs wiki, so the API
  reference is embedded in the documentation without a hand-maintained copy.
* The Swagger UI console FastAPI serves at ``/docs`` — ``--swagger-ui-dir``
  writes FastAPI's own ``get_swagger_ui_html`` page per service, pointing at a
  copy of the document and at a self-hosted ``swagger-ui-dist`` (the pinned npm
  dependency), so the Pages site keeps making zero third-party requests. That
  tree is built at deploy time by ``pages.yml`` and is not committed.

Everything is a deterministic function of the committed documents, so
freshness is verified by regenerating in memory and comparing (``--check``);
unlike the code flowcharts there is no rasteriser and nothing to stamp. The
render path is stdlib-only, which keeps ``python -S diagrams/generate.py
--check`` working without site-packages; FastAPI is imported only to build the
Swagger tree.

Usage::

    python diagrams/api_specs/generate.py                 # rewrite the sheets + README
    python diagrams/api_specs/generate.py --check         # exit 1 if any committed sheet is stale
    python diagrams/api_specs/generate.py --swagger-ui-dir site/swagger \\
        --swagger-assets node_modules/swagger-ui-dist     # build the interactive consoles
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import sys
from pathlib import Path
from typing import Any

#: The checkout this generator renders for; tests point the functions at a
#: temporary root instead, which is why every entry point takes one.
REPO_ROOT = Path(__file__).resolve().parents[2]

#: Path prefix of the Swagger UI consoles on the Pages site (``/swagger/<service>/``).
#: Distinct from the wiki's ``api/`` prefix, where the Markdown sheets live.
SWAGGER_SITE_PREFIX = "swagger"

#: The regeneration command every generated file names.
REGENERATION_COMMAND = "python diagrams/generate.py --api-only"

#: Files ``--swagger-ui-dir`` requires from the ``swagger-ui-dist`` package.
SWAGGER_ASSET_FILES: tuple[str, ...] = (
    "swagger-ui-bundle.js",
    "swagger-ui.css",
    "favicon-32x32.png",
)
#: Redistribution notices copied alongside the assets when the package ships them.
SWAGGER_NOTICE_FILES: tuple[str, ...] = ("LICENSE", "NOTICE", "swagger-ui-bundle.js.LICENSE.txt")

#: HTTP methods in the order a reader expects, not alphabetically.
_METHOD_ORDER: tuple[str, ...] = (
    "get",
    "post",
    "put",
    "patch",
    "delete",
    "head",
    "options",
    "trace",
)

_SITE_URL_RE = re.compile(r"^site_url:\s*(\S+)\s*$", re.MULTILINE)
_REPO_URL_RE = re.compile(r"^repo_url:\s*(\S+)\s*$", re.MULTILINE)
_REF_PREFIX = "#/components/schemas/"

#: The interaction diagram rendered next to the sheets.
TOPOLOGY_FILE = "api-topology.svg"

#: How each kind of document came to be, keyed by ``x-gco-source.kind`` (a
#: FastAPI export carries no source block). ``noun`` labels the sheet header,
#: ``origin`` finishes the sentence "the ... this sheet is rendered from".
_KINDS: dict[str, tuple[str, str]] = {
    "fastapi": (
        "Service",
        "FastAPI `app.openapi()` export (`scripts/generate_openapi.py`) this sheet is rendered from",
    ),
    "aws-api-gateway": (
        "API Gateway",
        "document `scripts/generate_api_gateway_openapi.py` reads out of the synthesized "
        "CDK stack; this sheet is rendered from it",
    ),
    "cluster-gateway": (
        "Gateway",
        "document `scripts/generate_cluster_gateway_openapi.py` composes from the HTTPRoute "
        "manifest and the service documents; this sheet is rendered from it",
    ),
}
#: Reading order of the catalogue index and the diagram: front doors first.
_KIND_ORDER: tuple[str, ...] = ("aws-api-gateway", "cluster-gateway", "fastapi")


class SpecSheetError(RuntimeError):
    """A document or the site configuration cannot be rendered faithfully."""


def _dict(value: Any) -> dict[str, Any]:
    """A JSON object as itself, anything else as an empty mapping."""
    return value if isinstance(value, dict) else {}


# ─── inputs ──────────────────────────────────────────────────────────────────


def service_names(openapi_dir: Path) -> list[str]:
    """The services with a committed OpenAPI document, by filename stem."""
    names = sorted(path.stem for path in openapi_dir.glob("*.json"))
    if not names:
        raise SpecSheetError(f"no OpenAPI documents under {openapi_dir}")
    return names


def load_document(service: str, openapi_dir: Path) -> dict[str, Any]:
    document = json.loads((openapi_dir / f"{service}.json").read_text(encoding="utf-8"))
    if not isinstance(document, dict) or not isinstance(document.get("paths"), dict):
        raise SpecSheetError(f"{service}: OpenAPI document has no paths object")
    return document


def document_kind(document: dict[str, Any]) -> str:
    """``fastapi``, ``aws-api-gateway`` or ``cluster-gateway`` — what produced the document."""
    kind = _dict(document.get("x-gco-source")).get("kind") or "fastapi"
    if kind not in _KINDS:
        raise SpecSheetError(f"unknown x-gco-source.kind {kind!r}")
    return str(kind)


def catalogue_order(documents: dict[str, dict[str, Any]]) -> list[str]:
    """Document names, front doors first, then alphabetical within a kind."""
    return sorted(
        documents, key=lambda name: (_KIND_ORDER.index(document_kind(documents[name])), name)
    )


def read_site_config(mkdocs_yml: Path) -> tuple[str, str]:
    """``(site_url, repo_url)`` from mkdocs.yml — the one place both are declared.

    Read with a regex rather than PyYAML so the render path stays stdlib-only,
    and so a fork's ``scripts/migrate_fork.py`` rewrite of mkdocs.yml is the
    only edit needed for the generated links to follow.
    """
    text = mkdocs_yml.read_text(encoding="utf-8")
    site = _SITE_URL_RE.search(text)
    repo = _REPO_URL_RE.search(text)
    if site is None or repo is None:
        raise SpecSheetError(f"{mkdocs_yml}: site_url and repo_url must both be declared")
    return site.group(1).rstrip("/") + "/", repo.group(1).rstrip("/")


# ─── Markdown primitives ─────────────────────────────────────────────────────


def slugify(heading: str) -> str:
    """GitHub-compatible heading anchor (python-markdown's toc agrees for ASCII).

    Lower-case, code spans unwrapped, anything but letters, digits, spaces,
    hyphens and underscores dropped, spaces to hyphens. ``GET /api/v1/jobs/{id}``
    becomes ``get-apiv1jobsid``.
    """
    text = heading.replace("`", "").lower()
    text = re.sub(r"[^a-z0-9 _-]", "", text)
    return re.sub(r"\s+", "-", text.strip())


def cell(text: object) -> str:
    """One table cell: whitespace collapsed, pipes escaped, empty shown as a dash."""
    collapsed = " ".join(str(text).split()) if text is not None else ""
    return collapsed.replace("|", "\\|") or "—"


def code(text: str) -> str:
    return f"`{text}`"


def table(headers: list[str], rows: list[list[str]]) -> list[str]:
    """A Markdown table; every row is padded to the header width (MD056)."""
    width = len(headers)
    lines = ["| " + " | ".join(headers) + " |", "|" + "---|" * width]
    for row in rows:
        padded = [*row, *(["—"] * (width - len(row)))][:width]
        lines.append("| " + " | ".join(padded) + " |")
    return lines


def _aligned_commands(*entries: tuple[str, str]) -> list[str]:
    """Shell lines whose trailing ``# comment`` starts in one column.

    The column is the longest command plus two spaces, so a renamed or added
    command re-aligns the whole block instead of overflowing a fixed width.
    """
    width = max(len(command) for command, _comment in entries)
    return [f"{command:<{width}}  # {comment}" for command, comment in entries]


# ─── schema rendering ────────────────────────────────────────────────────────


def _ref_name(ref: str) -> str:
    if not ref.startswith(_REF_PREFIX):
        raise SpecSheetError(f"unsupported $ref outside components/schemas: {ref}")
    return ref[len(_REF_PREFIX) :]


def schema_link(name: str) -> str:
    return f"[{code(name)}](#{schema_anchor(name)})"


def schema_type(schema: Any) -> str:
    """A compact, human-readable type for one JSON Schema fragment.

    Nullable unions (``anyOf`` with ``null``) read as ``string (nullable)``;
    references link to the schema's section; free-form objects and empty
    schemas are named as such rather than left blank.
    """
    if not isinstance(schema, dict):
        return "any"
    if "$ref" in schema:
        return schema_link(_ref_name(str(schema["$ref"])))
    for keyword, joiner in (("anyOf", " or "), ("oneOf", " or "), ("allOf", " and ")):
        variants = schema.get(keyword)
        if isinstance(variants, list) and variants:
            non_null = [
                v for v in variants if not (isinstance(v, dict) and v.get("type") == "null")
            ]
            nullable = len(non_null) < len(variants)
            rendered = joiner.join(schema_type(v) for v in non_null) or "null"
            return f"{rendered} (nullable)" if nullable else rendered
    if "enum" in schema and isinstance(schema["enum"], list):
        values = ", ".join(code(json.dumps(v)) for v in schema["enum"])
        return f"enum: {values}"
    if "const" in schema:
        return f"const {code(json.dumps(schema['const']))}"
    declared = schema.get("type")
    types = declared if isinstance(declared, list) else [declared] if declared else []
    if not types:
        return (
            "object (free-form)"
            if "additionalProperties" in schema or "properties" in schema
            else "any"
        )
    parts: list[str] = []
    for type_name in types:
        if type_name == "array":
            parts.append(f"array of {schema_type(schema.get('items', {}))}")
        elif type_name == "object":
            additional = schema.get("additionalProperties")
            if isinstance(additional, dict) and additional:
                parts.append(f"object of {schema_type(additional)}")
            elif additional is True or (additional == {} and "properties" not in schema):
                parts.append("object (free-form)")
            else:
                parts.append("object")
        elif type_name == "string" and schema.get("format"):
            parts.append(f"string ({schema['format']})")
        else:
            parts.append(str(type_name))
    return " or ".join(parts)


_CONSTRAINT_KEYS: tuple[tuple[str, str], ...] = (
    ("minimum", "≥ {}"),
    ("exclusiveMinimum", "> {}"),
    ("maximum", "≤ {}"),
    ("exclusiveMaximum", "< {}"),
    ("minLength", "min length {}"),
    ("maxLength", "max length {}"),
    ("minItems", "min items {}"),
    ("maxItems", "max items {}"),
    ("pattern", "pattern {}"),
    ("multipleOf", "multiple of {}"),
)


def _constraint_fragments(schema: Any) -> list[str]:
    if not isinstance(schema, dict):
        return []
    found = [
        template.format(code(str(schema[key])) if key == "pattern" else schema[key])
        for key, template in _CONSTRAINT_KEYS
        if key in schema
    ]
    # FastAPI puts the bounds of an ``Optional[...]`` field on the non-null
    # variant of its ``anyOf``, so a nullable ``str`` with ``max_length`` is
    # only described if the variants are read too.
    for keyword in ("anyOf", "oneOf", "allOf"):
        variants = schema.get(keyword)
        if isinstance(variants, list):
            for variant in variants:
                for fragment in _constraint_fragments(variant):
                    if fragment not in found:
                        found.append(fragment)
    return found


def constraints(schema: Any) -> str:
    """Numeric/length/pattern bounds as ``≥ 1; ≤ 1000``, or a dash."""
    return cell("; ".join(_constraint_fragments(schema)))


def default_of(schema: Any) -> str:
    if isinstance(schema, dict) and "default" in schema:
        return code(json.dumps(schema["default"]))
    return "—"


def _description(*candidates: Any) -> str:
    """The first non-empty description among the candidates, as one table cell."""
    for candidate in candidates:
        if isinstance(candidate, str) and candidate.strip():
            return cell(candidate)
    return "—"


def _prose(text: Any) -> list[str]:
    """A description as its own paragraph(s), trailing whitespace trimmed."""
    if not isinstance(text, str) or not text.strip():
        return []
    return [line.rstrip() for line in text.strip().splitlines()]


# ─── operations ──────────────────────────────────────────────────────────────


def _operations(document: dict[str, Any]) -> list[tuple[str, str, dict[str, Any]]]:
    """``(method, path, operation)`` in path order, methods in reading order."""
    found: list[tuple[str, str, dict[str, Any]]] = []
    for path, item in document["paths"].items():
        if not isinstance(item, dict):
            continue
        for method in _METHOD_ORDER:
            operation = item.get(method)
            if isinstance(operation, dict):
                found.append((method.upper(), str(path), operation))
    return found


def operation_heading(method: str, path: str) -> str:
    return f"{method} {path}"


def operation_anchor(method: str, path: str) -> str:
    """``op-get-api-v1-jobs-namespace-name``: an explicit anchor for the operation.

    Headings get an ``<a id>`` of our own rather than relying on the renderer's
    heading slug: GitHub and python-markdown agree on ``GET /api/v1/jobs`` but
    not on the root path ``GET /`` (GitHub keeps a trailing hyphen, Markdown
    strips it), and an anchor both honour is worth one extra element.
    """
    tail = re.sub(r"[^a-z0-9]+", "-", path.lower()).strip("-")
    return f"op-{method.lower()}" + (f"-{tail}" if tail else "")


def schema_anchor(name: str) -> str:
    return f"schema-{slugify(name)}"


def anchored_heading(anchor: str, heading: str) -> list[str]:
    return [f'<a id="{anchor}"></a>', "", f"### {code(heading)}", ""]


def _content_summary(content: Any) -> str:
    """``application/json: <type>`` for each media type of a body or response."""
    if not isinstance(content, dict) or not content:
        return "—"
    return cell(
        "; ".join(
            f"{code(media_type)}: {schema_type(media.get('schema') if isinstance(media, dict) else None)}"
            for media_type, media in content.items()
        )
    )


def render_operation(method: str, path: str, operation: dict[str, Any]) -> list[str]:
    lines = anchored_heading(operation_anchor(method, path), operation_heading(method, path))
    lines.extend(_prose(operation.get("description") or operation.get("summary")))
    if lines[-1] != "":
        lines.append("")
    facts = []
    if operation.get("operationId"):
        facts.append(f"- **Operation ID:** {code(str(operation['operationId']))}")
    tags = operation.get("tags")
    if isinstance(tags, list) and tags:
        facts.append(f"- **Tags:** {', '.join(str(tag) for tag in tags)}")
    if operation.get("deprecated") is True:
        facts.append("- **Deprecated:** yes")
    facts.extend(_security_facts(operation.get("security")))
    facts.extend(_integration_facts(operation.get("x-amazon-apigateway-integration")))
    facts.extend(_route_facts(operation.get("x-gco-route")))
    if facts:
        lines.extend([*facts, ""])
    lines.extend(_backend_block(operation.get("x-gco-backend")))

    parameters = operation.get("parameters")
    if isinstance(parameters, list) and parameters:
        rows = []
        for parameter in parameters:
            if not isinstance(parameter, dict):
                continue
            schema = parameter.get("schema")
            rows.append(
                [
                    code(str(parameter.get("name", ""))),
                    cell(parameter.get("in")),
                    schema_type(schema),
                    "yes" if parameter.get("required") is True else "no",
                    default_of(schema),
                    constraints(schema),
                    _description(
                        parameter.get("description"),
                        schema.get("description") if isinstance(schema, dict) else None,
                    ),
                ]
            )
        lines.append("**Parameters**")
        lines.append("")
        lines.extend(
            table(["Name", "In", "Type", "Required", "Default", "Constraints", "Description"], rows)
        )
        lines.append("")

    body = operation.get("requestBody")
    if isinstance(body, dict):
        required = "required" if body.get("required") is True else "optional"
        lines.append(f"**Request body** ({required}): {_content_summary(body.get('content'))}")
        lines.append("")

    responses = operation.get("responses")
    if isinstance(responses, dict) and responses:
        rows = [
            [
                code(str(status)),
                _description(response.get("description") if isinstance(response, dict) else None),
                _content_summary(response.get("content") if isinstance(response, dict) else None),
            ]
            for status, response in sorted(responses.items(), key=lambda item: str(item[0]))
        ]
        lines.append("**Responses**")
        lines.append("")
        lines.extend(table(["Status", "Description", "Content"], rows))
        lines.append("")
    return lines


def _security_facts(security: Any) -> list[str]:
    """``- **Security:** ...`` when the operation declares its requirements.

    FastAPI exports omit the key (authentication is middleware, not schema);
    the gateway documents write it explicitly, ``[]`` meaning unauthenticated.
    """
    if not isinstance(security, list):
        return []
    if not security:
        return ["- **Security:** none (unauthenticated)"]
    names = sorted(
        {
            str(name)
            for requirement in security
            if isinstance(requirement, dict)
            for name in requirement
        }
    )
    return [f"- **Security:** {', '.join(code(name) for name in names)}"]


def _integration_facts(integration: Any) -> list[str]:
    """API Gateway's integration type, timeout and transfer mode as one line."""
    if not isinstance(integration, dict):
        return []
    parts = [code(str(integration.get("type", "?")))]
    timeout = integration.get("timeoutInMillis")
    if isinstance(timeout, int):
        parts.append(f"{timeout / 1000:g} s integration timeout")
    if integration.get("responseTransferMode") == "STREAM":
        parts.append("response streaming")
    return [f"- **Integration:** {', '.join(parts)}"]


def _route_facts(route: Any) -> list[str]:
    """The HTTPRoute rule that delivers an operation of the cluster gateway."""
    if not isinstance(route, dict):
        return []
    backend = f"{route.get('service')}:{route.get('port')}"
    return [
        f"- **Delivered by rule:** {code(str(route.get('rule')))} → Service "
        f"{code(backend)} in {code(str(route.get('namespace')))}"
    ]


def _backend_block(backend: Any) -> list[str]:
    """The Lambda behind an API Gateway operation and the hops that follow it."""
    if not isinstance(backend, dict):
        return []
    lines = ["**Backend**", ""]
    function = backend.get("lambda")
    if isinstance(function, dict):
        detail = ", ".join(
            str(function[key]) for key in ("runtime", "handler") if function.get(key) is not None
        )
        timeout = function.get("timeoutSeconds")
        if isinstance(timeout, int):
            detail += f", {timeout} s timeout"
        lines.append(
            f"- **Lambda:** {code(str(function.get('name')))} "
            f"({code(str(function.get('source')))}; {detail})"
        )
    else:
        lines.append(f"- **Integration:** {code(str(backend.get('integration')))}")
    description = backend.get("description")
    if isinstance(description, str) and description.strip():
        lines.append(f"- **Behaviour:** {cell(description)}")
    hops = backend.get("hops")
    valid_hops = [hop for hop in hops if isinstance(hop, dict)] if isinstance(hops, list) else []
    if valid_hops:
        lines.append("- **Then:**")
        for index, hop in enumerate(valid_hops, start=1):
            documents = hop.get("documents")
            links = (
                " — " + ", ".join(f"[{code(str(name))}]({name}.md)" for name in documents)
                if isinstance(documents, list) and documents
                else ""
            )
            lines.append(f"    {index}. {cell(hop.get('label'))}{links}")
    lines.append("")
    return lines


# ─── document-level extensions ───────────────────────────────────────────────


def _json_block(value: Any) -> list[str]:
    return ["```json", json.dumps(value, indent=2, sort_keys=True), "```", ""]


def render_servers(servers: Any) -> list[str]:
    """``## Servers``: each URL template and its variables."""
    if not isinstance(servers, list) or not servers:
        return []
    lines = ["## Servers", ""]
    for server in servers:
        if not isinstance(server, dict):
            continue
        lines.append(f"- {code(str(server.get('url')))}")
        if server.get("description"):
            lines.extend(["", *(f"  {line}" for line in _prose(server.get("description")))])
        variables = server.get("variables")
        if isinstance(variables, dict) and variables:
            lines.append("")
            rows = [
                [
                    code(str(name)),
                    code(json.dumps(spec.get("default")))
                    if isinstance(spec, dict) and "default" in spec
                    else "—",
                    _description(spec.get("description") if isinstance(spec, dict) else None),
                ]
                for name, spec in variables.items()
            ]
            lines.extend(f"  {row}" for row in table(["Variable", "Default", "Description"], rows))
        lines.append("")
    return lines


def render_security_schemes(schemes: Any) -> list[str]:
    """``## Security schemes``: how a caller authenticates to this surface."""
    if not isinstance(schemes, dict) or not schemes:
        return []
    lines = ["## Security schemes", ""]
    for name, scheme in schemes.items():
        if not isinstance(scheme, dict):
            continue
        where = ""
        if scheme.get("in") and scheme.get("name"):
            where = f" — {scheme['in']} {code(str(scheme['name']))}"
        authtype = scheme.get("x-amazon-apigateway-authtype")
        if authtype:
            where += f" ({authtype})"
        lines.append(f"### {code(str(name))}")
        lines.append("")
        lines.append(f"- **Type:** {cell(scheme.get('type'))}{where}")
        if scheme.get("description"):
            lines.extend(["", *_prose(scheme.get("description"))])
        lines.append("")
    return lines


def render_gateway_details(document: dict[str, Any]) -> list[str]:
    """``## Deployment``: the API Gateway facts the synthesized template carried."""
    stage = document.get("x-gco-stage")
    policy = document.get("x-gco-resource-policy")
    if not isinstance(stage, dict) and not isinstance(policy, list):
        return []
    lines = ["## Deployment", ""]
    endpoint_type = document.get("x-gco-endpoint-type")
    if endpoint_type:
        lines.append(f"- **Endpoint type:** {code(str(endpoint_type))}")
    if isinstance(stage, dict):
        throttling = _dict(stage.get("throttling"))
        lines.append(
            f"- **Stage:** {code(str(stage.get('name')))} — throttling "
            f"{throttling.get('rateLimit')} req/s (burst {throttling.get('burstLimit')}), "
            f"execution logging {code(str(stage.get('loggingLevel')))}, data trace "
            f"{'on' if stage.get('dataTraceEnabled') else 'off'}, metrics "
            f"{'on' if stage.get('metricsEnabled') else 'off'}, X-Ray tracing "
            f"{'on' if stage.get('tracingEnabled') else 'off'}, access logs "
            f"{'on' if stage.get('accessLogging') else 'off'}"
        )
    conditions = document.get("x-gco-conditions")
    if isinstance(conditions, list) and conditions:
        lines.append("- **Conditional routes:** operations marked *Only when* exist under:")
        for condition in conditions:
            if isinstance(condition, dict):
                lines.append(
                    f"  - {code(str(condition.get('id')))}: {cell(condition.get('description'))}"
                )
    lines.append("")
    if isinstance(policy, list):
        lines.extend(["### Resource policy", ""])
        lines.append(
            "Statements deployed on the REST API (`${AWS::AccountId}` is the deploying account):"
        )
        lines.append("")
        lines.extend(_json_block(policy))
        direct = document.get("x-gco-resource-policy-direct-access")
        if isinstance(direct, list) and direct:
            lines.append(
                "Added when `api_gateway.regional_api_enabled` is true (and always outside "
                "the `aws` partition), admitting the deploying account's other principals:"
            )
            lines.append("")
            lines.extend(_json_block(direct))
    waf = document.get("x-gco-waf")
    if isinstance(waf, list) and waf:
        lines.extend(["### WAF", ""])
        rows = [
            [
                str(rule.get("priority")),
                code(str(rule.get("name"))),
                cell(rule.get("action")),
                code(str(rule["managedRuleGroup"])) if rule.get("managedRuleGroup") else "—",
            ]
            for rule in waf
            if isinstance(rule, dict)
        ]
        lines.extend(table(["Priority", "Rule", "Action", "Managed rule group"], rows))
        lines.append("")
    return lines


def render_cluster_gateway_details(document: dict[str, Any]) -> list[str]:
    """``## Routing``: the Gateway, its HTTPRoute table and what it leaves out."""
    gateway = document.get("x-gco-gateway")
    routes = document.get("x-gco-routes")
    if not isinstance(gateway, dict) or not isinstance(routes, list):
        return []
    lines = ["## Routing", ""]
    listener = _dict(gateway.get("listener"))
    load_balancer = _dict(gateway.get("loadBalancer"))
    qualified = f"{gateway.get('namespace')}/{gateway.get('name')}"
    lines.append(
        f"- **Gateway:** {code(qualified)} "
        f"(GatewayClass {code(str(gateway.get('gatewayClassName')))}, controller "
        f"{code(str(gateway.get('controllerName')))})"
    )
    lines.append(
        f"- **Listener:** {code(str(listener.get('name')))} "
        f"{listener.get('protocol')}:{listener.get('port')}; load balancer scheme "
        f"{code(str(load_balancer.get('scheme')))}"
    )
    lines.append(f"- **HTTPRoute:** {code(str(gateway.get('httpRoute')))}")
    unauthenticated = document.get("x-gco-unauthenticated-paths")
    if isinstance(unauthenticated, list) and unauthenticated:
        lines.append(
            "- **Paths served without the HMAC envelope:** "
            + ", ".join(code(str(path)) for path in unauthenticated)
        )
    lines.append("")
    lines.extend(["### HTTPRoute rules", ""])
    lines.append(
        "In document order; the controller awards precedence by path specificity "
        "(longest prefix wins), so `/` is the catch-all."
    )
    lines.append("")
    rows = [
        [code(str(rule.get("prefix"))), code(str(rule.get("service"))), str(rule.get("port"))]
        for rule in routes
        if isinstance(rule, dict)
    ]
    lines.extend(table(["Path prefix", "Service", "Port"], rows))
    lines.append("")
    for key, heading in (
        ("loadBalancer", "Load balancer"),
        ("targetGroup", "Target group"),
    ):
        value = gateway.get(key)
        if isinstance(value, dict) and value:
            lines.extend([f"### {heading}", "", *_json_block(value)])
    unreachable = document.get("x-gco-unreachable")
    if isinstance(unreachable, list) and unreachable:
        lines.extend(["### Paths a Service serves but never receives", ""])
        lines.append(
            "A prefix cannot be split between Services, so these implementations are "
            "reachable only by addressing the Service inside the cluster."
        )
        lines.append("")
        rows = [
            [
                code(str(item.get("path"))),
                code(str(item.get("service"))),
                code(str(item.get("rule"))),
                code(str(item.get("deliveredTo"))),
            ]
            for item in unreachable
            if isinstance(item, dict)
        ]
        lines.extend(table(["Path", "Implemented by", "Winning rule", "Delivered to"], rows))
        lines.append("")
    not_exposed = document.get("x-gco-not-exposed")
    if isinstance(not_exposed, dict) and not_exposed:
        lines.extend(["### Services not on the ALB", ""])
        for service, detail in not_exposed.items():
            info = _dict(detail)
            lines.append(
                f"- [{code(str(service))}]({service}.md) — reached through "
                f"[{code(str(info.get('via')))}]({info.get('via')}.md) "
                f"({code(str(info.get('pathPrefix')) + '*')}). {cell(info.get('description'))}"
            )
        lines.append("")
    downstream = document.get("x-gco-downstream")
    if isinstance(downstream, dict) and downstream:
        lines.extend(["### Downstream of the backends", ""])
        for service, detail in downstream.items():
            info = _dict(detail)
            lines.append(
                f"- [{code(str(service))}]({service}.md) → {cell(info.get('target'))}. "
                f"{cell(info.get('description'))}"
            )
        lines.append("")
    return lines


# ─── schemas ─────────────────────────────────────────────────────────────────


def render_schema(name: str, schema: dict[str, Any]) -> list[str]:
    lines = anchored_heading(schema_anchor(name), name)
    lines.extend(_prose(schema.get("description")))
    if lines[-1] != "":
        lines.append("")
    properties = schema.get("properties")
    required = schema.get("required")
    required_names = {str(item) for item in required} if isinstance(required, list) else set()
    if isinstance(properties, dict) and properties:
        rows = [
            [
                code(str(prop_name)),
                schema_type(prop),
                "yes" if prop_name in required_names else "no",
                default_of(prop),
                constraints(prop),
                _description(prop.get("description") if isinstance(prop, dict) else None),
            ]
            for prop_name, prop in properties.items()
        ]
        lines.extend(
            table(["Property", "Type", "Required", "Default", "Constraints", "Description"], rows)
        )
        lines.append("")
    else:
        lines.append(f"- **Type:** {schema_type(schema)}")
        lines.append("")
    if "example" in schema:
        lines.extend(
            ["Example:", "", "```json", json.dumps(schema["example"], indent=2), "```", ""]
        )
    return lines


# ─── the sheet ───────────────────────────────────────────────────────────────


def banner(service: str) -> str:
    return (
        f"<!-- Generated by diagrams/api_specs/generate.py from docs/openapi/{service}.json. "
        f"Do not edit by hand; regenerate with `{REGENERATION_COMMAND}`. -->"
    )


def _assert_unique_anchors(anchors: list[tuple[str, str]]) -> None:
    """``(anchor, owner)`` pairs must not collide, or a link would land on the wrong section."""
    seen: dict[str, str] = {}
    for anchor, owner in anchors:
        if anchor in seen:
            raise SpecSheetError(
                f"{seen[anchor]!r} and {owner!r} share the anchor #{anchor}; "
                "the summary table could not link to both"
            )
        seen[anchor] = owner


def render_sheet(service: str, document: dict[str, Any], *, site_url: str, repo_url: str) -> str:
    info = _dict(document.get("info"))
    title = str(info.get("title") or service)
    operations = _operations(document)
    schemas = _dict(_dict(document.get("components")).get("schemas"))
    _assert_unique_anchors(
        [
            (operation_anchor(method, path), operation_heading(method, path))
            for method, path, _ in operations
        ]
        + [(schema_anchor(str(name)), str(name)) for name in schemas]
    )

    noun, origin = _KINDS[document_kind(document)]

    lines = [f"# {title} — API spec sheet", "", banner(service), ""]
    lines.append(
        f"*{noun} {code(service)} · OpenAPI {document.get('openapi', '?')} · "
        f"API version {info.get('version', '?')} · {len(operations)} endpoints · {len(schemas)} schemas*"
    )
    lines.append("")
    lines.extend(_prose(info.get("description")))
    if lines[-1] != "":
        lines.append("")
    lines.extend(
        [
            f"- **Machine-readable document:** [{code(f'docs/openapi/{service}.json')}]"
            f"({repo_url}/blob/main/docs/openapi/{service}.json) — the {origin}",
            f"- **Interactive console (Swagger UI):** <{site_url}{SWAGGER_SITE_PREFIX}/{service}/>",
            "- **Catalogue index:** [README.md](README.md) · "
            "[interaction diagram](README.md#how-the-surfaces-fit-together)",
            "",
        ]
    )
    lines.extend(render_servers(document.get("servers")))
    lines.extend(render_gateway_details(document))
    lines.extend(render_cluster_gateway_details(document))
    lines.extend(render_security_schemes(_dict(document.get("components")).get("securitySchemes")))
    lines.extend(["## Endpoints", ""])
    rows = []
    for method, path, operation in operations:
        anchor = operation_anchor(method, path)
        tags = operation.get("tags")
        rows.append(
            [
                f"[{code(method)}](#{anchor})",
                code(path),
                _description(operation.get("summary")),
                cell(", ".join(str(tag) for tag in tags))
                if isinstance(tags, list) and tags
                else "—",
            ]
        )
    lines.extend(table(["Method", "Path", "Summary", "Tags"], rows))
    lines.extend(["", "## Endpoint details", ""])
    for method, path, operation in operations:
        lines.extend(render_operation(method, path, operation))
    lines.extend(["## Schemas", ""])
    if schemas:
        for name, schema in schemas.items():
            lines.extend(render_schema(str(name), schema if isinstance(schema, dict) else {}))
    else:
        lines.extend([f"This {noun.lower()} declares no component schemas.", ""])
    return "\n".join(lines).rstrip() + "\n"


def render_index(documents: dict[str, dict[str, Any]], *, site_url: str, repo_url: str) -> str:
    rows = []
    for service in catalogue_order(documents):
        document = documents[service]
        info = _dict(document.get("info"))
        rows.append(
            [
                f"[{code(service)}]({service}.md)",
                _KINDS[document_kind(document)][0],
                cell(info.get("title")),
                code(str(info.get("version", "?"))),
                str(len(_operations(document))),
                f"[{code('json')}]({repo_url}/blob/main/docs/openapi/{service}.json)",
                f"[{code('/docs')}]({site_url}{SWAGGER_SITE_PREFIX}/{service}/)",
            ]
        )
    lines = [
        "# GCO API Spec Sheets",
        "",
        "<!-- Generated by diagrams/api_specs/generate.py from docs/openapi/*.json. "
        f"Do not edit by hand; regenerate with `{REGENERATION_COMMAND}`. -->",
        "",
        "One spec sheet per HTTP surface of GCO, rendered from the OpenAPI document that",
        "describes it: for each FastAPI **service** the document the application",
        "generates itself (the one behind its automatic Swagger `/docs` page); for each",
        "AWS **API Gateway** the document read out of the synthesized CDK stack; and for",
        "the in-cluster **Gateway** (the internal ALB) the document composed from its",
        "HTTPRoute and the service documents. The documents are committed under",
        "`docs/openapi/`, each guarded by a `--check` in CI, and these sheets are a",
        "deterministic rendering of them — so a route, parameter or model change shows up",
        "here in the same pull request as the code.",
        "",
        *table(
            ["Document", "Kind", "API", "Version", "Endpoints", "OpenAPI document", "Swagger UI"],
            rows,
        ),
        "",
        "The **Swagger UI** column is the Swagger console for each document (FastAPI's own",
        f"`/docs` page for the services), served from the project site under `/{SWAGGER_SITE_PREFIX}/`",
        "with a self-hosted copy of `swagger-ui-dist` (the site makes no third-party",
        "requests). It is built at deploy time by `pages.yml`; the sheets in this directory",
        "are also injected into the wiki under `/api/` by `scripts/mkdocs_hooks.py`, so",
        "both renderings come from one source.",
        "",
        "## How the surfaces fit together",
        "",
        f"![How the API Gateways, the cluster Gateway and the services interact]({TOPOLOGY_FILE})",
        "",
        "Drawn from the same documents: a client signs a request with SigV4 to one of the",
        "two API Gateways; a Lambda proxy adds the request-bound HMAC envelope and forwards",
        "it — over Global Accelerator from the global API, inside the VPC from the regional",
        "bridge — to the region's internal ALB, the Kubernetes Gateway whose HTTPRoute picks",
        "the Service by longest path prefix. The aggregator answers `/api/v1/global/*` by",
        "fanning out to the regional API Gateways. Dashed arrows are opt-in or dynamic",
        "paths; `†` marks routes that exist only under a deployment condition. Every box",
        "is a document in the table above, and each is a link to its sheet on the site.",
        "",
        "## Regenerating",
        "",
        "```bash",
        *_aligned_commands(
            ("python scripts/generate_openapi.py", "refresh the service documents from the apps"),
            (
                "python scripts/generate_api_gateway_openapi.py",
                "re-synthesize the API Gateway documents",
            ),
            (
                "python scripts/generate_cluster_gateway_openapi.py",
                "recompose the cluster gateway document",
            ),
            (REGENERATION_COMMAND, "rewrite these sheets, this index and the diagram"),
            ("python diagrams/generate.py --check", "fail if anything here is stale"),
        ),
        "```",
        "",
        "To browse the consoles locally, install the locked npm tooling and serve the",
        "generated tree:",
        "",
        "```bash",
        "npm ci --ignore-scripts --no-audit --no-fund",
        f"python diagrams/api_specs/generate.py --swagger-ui-dir /tmp/gco-{SWAGGER_SITE_PREFIX} \\",
        "    --swagger-assets node_modules/swagger-ui-dist",
        f"python -m http.server --directory /tmp/gco-{SWAGGER_SITE_PREFIX} 8080",
        "```",
        "",
        "Swagger UI loads the document with a browser request, so the tree has to be",
        "served over HTTP rather than opened from the filesystem.",
    ]
    return "\n".join(lines) + "\n"


# ─── the interaction diagram ─────────────────────────────────────────────────
#
# ``api-topology.svg`` is drawn from the documents alone: the API Gateway
# documents' ``x-gco-backend`` hop chains, the cluster gateway's ``x-gco-routes``
# (plus what it declares not exposed or downstream) and the service documents.
# The layout is a fixed grid of columns — clients, API Gateways, Lambda
# proxies, transit, the cluster Gateway, Services, what sits behind them — so
# the picture is a deterministic function of the JSON and ``--check`` can
# compare it byte for byte. Every box that is a document links to its sheet.

_SVG_FONT = "ui-sans-serif, system-ui, -apple-system, 'Segoe UI', Helvetica, Arial, sans-serif"
_SVG_MONO = "ui-monospace, SFMono-Regular, Menlo, Consolas, monospace"
#: ``(fill, stroke)`` per node style.
_NODE_STYLES: dict[str, tuple[str, str]] = {
    "clients": ("#ffffff", "#334155"),
    "api-gateway": ("#dbeafe", "#1d4ed8"),
    "lambda": ("#fef3c7", "#b45309"),
    "transit": ("#ede9fe", "#6d28d9"),
    "cluster-gateway": ("#dcfce7", "#15803d"),
    "service": ("#f1f5f9", "#334155"),
    "dynamic": ("#ffffff", "#64748b"),
}
_LEGEND: tuple[tuple[str, str], ...] = (
    ("api-gateway", "AWS API Gateway (SigV4)"),
    ("lambda", "Lambda proxy (HMAC envelope)"),
    ("transit", "AWS transit"),
    ("cluster-gateway", "Kubernetes Gateway (internal ALB)"),
    ("service", "FastAPI service"),
    ("dynamic", "dynamic or internal-only"),
)
_COLUMN_WIDTHS: tuple[int, ...] = (150, 250, 225, 180, 265, 205, 215)
#: Space after each column; wider where edge labels (route prefixes) sit.
_COLUMN_GAPS: tuple[int, ...] = (130, 175, 60, 60, 140, 130, 0)
_MARGIN = 20
_NODE_GAP = 26
_TITLE_HEIGHT = 30
_LINE_HEIGHT = 15
_CHAR_WIDTH = 6.3
_EDGE_COLOUR = "#475569"
_FANOUT_COLOUR = "#6d28d9"


class _Node:
    def __init__(
        self,
        node_id: str,
        column: int,
        title: str,
        lines: list[str],
        style: str,
        href: str | None = None,
    ) -> None:
        self.id = node_id
        self.column = column
        self.title = title
        self.lines = lines
        self.style = style
        self.href = href
        self.x = 0
        self.y = 0
        self.width = _COLUMN_WIDTHS[column]
        self.height = _TITLE_HEIGHT + _LINE_HEIGHT * len(lines) + 10


class _Edge:
    def __init__(
        self,
        source: str,
        target: str,
        label: str = "",
        dashed: bool = False,
        colour: str = _EDGE_COLOUR,
    ) -> None:
        self.source = source
        self.target = target
        self.labels = [label] if label else []
        self.dashed = dashed
        self.colour = colour


def _wrap(text: str, width: int, *, indent: str = "") -> list[str]:
    """Break ``text`` into lines that fit a node of ``width`` pixels in the mono font."""
    import textwrap

    chars = max(12, int((width - 18) / _CHAR_WIDTH))
    return textwrap.wrap(str(text), width=chars, subsequent_indent=indent) or [""]


def _route_lines(document: dict[str, Any], width: int) -> list[str]:
    """One entry per path of an API Gateway document: its methods, then ``†`` if conditional."""
    lines: list[str] = []
    for path, item in document["paths"].items():
        if not isinstance(item, dict):
            continue
        methods = [m.upper() for m in _METHOD_ORDER if isinstance(item.get(m), dict)]
        conditional = any("x-gco-condition" in item[m.lower()] for m in methods)
        text = f"{' '.join(methods)} {path}{' †' if conditional else ''}"
        lines.extend(_wrap(text, width, indent="  "))
    return lines


def _short_title(hop_id: str) -> str:
    return " ".join(word.capitalize() for word in hop_id.split("-"))


def topology_model(
    documents: dict[str, dict[str, Any]], *, site_url: str
) -> tuple[list[_Node], list[_Edge]]:
    """Nodes and edges of the interaction diagram, derived from the documents."""
    kinds = {name: document_kind(document) for name, document in documents.items()}
    gateways = [n for n in catalogue_order(documents) if kinds[n] == "aws-api-gateway"]
    clusters = [n for n in catalogue_order(documents) if kinds[n] == "cluster-gateway"]
    services = [n for n in catalogue_order(documents) if kinds[n] == "fastapi"]
    nodes: dict[str, _Node] = {}
    edges: dict[tuple[str, str], _Edge] = {}

    def link(name: str) -> str:
        return f"{site_url}api/{name}/"

    def connect(
        source: str,
        target: str,
        label: str = "",
        *,
        dashed: bool = False,
        colour: str = _EDGE_COLOUR,
    ) -> None:
        for node_id in (source, target):
            if node_id not in nodes:
                raise SpecSheetError(f"the diagram references an unknown node {node_id!r}")
        edge = edges.setdefault(
            (source, target), _Edge(source, target, dashed=dashed, colour=colour)
        )
        if label and label not in edge.labels:
            edge.labels.append(label)

    nodes["clients"] = _Node(
        "clients",
        0,
        "Clients",
        ["AWS SigV4 (IAM)", "gco CLI, awscurl,", "boto3, curl --aws-sigv4"],
        "clients",
    )
    not_exposed: dict[str, dict[str, Any]] = {}
    for name in clusters:
        for service, info in _dict(documents[name].get("x-gco-not-exposed")).items():
            not_exposed[str(service)] = _dict(info)
    for name in services:
        document = documents[name]
        column = 6 if name in not_exposed else 5
        lines = [f"{len(_operations(document))} endpoints", f"Service {name}"]
        nodes[f"doc:{name}"] = _Node(
            f"doc:{name}",
            column,
            str(_dict(document.get("info")).get("title") or name),
            lines,
            "dynamic" if name in not_exposed else "service",
            link(name),
        )
    for name in gateways:
        document = documents[name]
        nodes[f"doc:{name}"] = _Node(
            f"doc:{name}",
            1,
            str(_dict(document.get("info")).get("title") or name),
            _route_lines(document, _COLUMN_WIDTHS[1]),
            "api-gateway",
            link(name),
        )
    for name in clusters:
        document = documents[name]
        gateway = _dict(document.get("x-gco-gateway"))
        routes = [r for r in document.get("x-gco-routes", []) if isinstance(r, dict)]
        lines = [
            f"Gateway {gateway.get('namespace')}/{gateway.get('name')}",
            f"HTTPRoute {gateway.get('httpRoute')} (prefix → Service)",
            *[f"{r.get('prefix')} → {r.get('service')}" for r in routes],
        ]
        nodes[f"doc:{name}"] = _Node(
            f"doc:{name}",
            4,
            str(_dict(document.get("info")).get("title") or name),
            [line for text in lines for line in _wrap(text, _COLUMN_WIDTHS[4], indent="  ")],
            "cluster-gateway",
            link(name),
        )

    # What the backends send on to (declared by the cluster gateway) is drawn
    # once, under the hop id the API Gateway chains use for the same place.
    for name in clusters:
        for service, detail in _dict(documents[name].get("x-gco-downstream")).items():
            info = _dict(detail)
            node_id = f"hop:{info.get('hop') or service}"
            nodes[node_id] = _Node(
                node_id,
                6,
                _short_title(str(info.get("hop") or service)) + "s",
                _wrap(str(info.get("target") or ""), _COLUMN_WIDTHS[6])[:4],
                "dynamic",
            )

    # Lambda nodes first, so a hop that names another API's proxy Lambda
    # (the aggregator's fan-out ends at the regional bridge's proxy) resolves.
    lambdas_by_name: dict[str, str] = {}
    for name in gateways:
        for _method, _path, operation in _operations(documents[name]):
            function = _dict(_dict(operation.get("x-gco-backend")).get("lambda"))
            if not function:
                continue
            node_id = f"lambda:{name}:{function.get('name')}"
            if node_id not in nodes:
                source = str(function.get("source") or "")
                nodes[node_id] = _Node(
                    node_id,
                    2,
                    str(function.get("name")),
                    [f"Lambda · {source.rsplit('/', 1)[-1]}", str(function.get("runtime") or "")],
                    "lambda",
                )
            lambdas_by_name.setdefault(str(function.get("name")), node_id)

    def hop_targets(hop: dict[str, Any], previous: _Node) -> list[str]:
        named = hop.get("documents")
        if isinstance(named, list) and named:
            return [f"doc:{document}" for document in named]
        hop_id = str(hop.get("id"))
        if hop_id in lambdas_by_name:
            return [lambdas_by_name[hop_id]]
        node_id = f"hop:{hop_id}"
        if node_id not in nodes:
            column = max(3, previous.column + 1)
            nodes[node_id] = _Node(
                node_id,
                column,
                _short_title(hop_id),
                _wrap(str(hop.get("label") or ""), _COLUMN_WIDTHS[column])[:4],
                "dynamic" if column >= 5 else "transit",
            )
        return [node_id]

    for name in gateways:
        document = documents[name]
        direct = bool(document.get("x-gco-resource-policy-direct-access"))
        connect("clients", f"doc:{name}", "SigV4 · opt-in" if direct else "SigV4", dashed=direct)
        # One label per path into a Lambda; a path is marked conditional when
        # any of its methods is.
        conditional_paths = {
            path for _m, path, operation in _operations(document) if "x-gco-condition" in operation
        }
        for _method, path, operation in _operations(document):
            backend = _dict(operation.get("x-gco-backend"))
            function = _dict(backend.get("lambda"))
            if not function:
                continue
            previous_id = f"lambda:{name}:{function.get('name')}"
            marker = " †" if path in conditional_paths else ""
            connect(f"doc:{name}", previous_id, f"{path}{marker}")
            for hop in backend.get("hops", []):
                if not isinstance(hop, dict):
                    continue
                targets = hop_targets(hop, nodes[previous_id])
                fan_out = str(hop.get("id")) in {n.removeprefix("doc:") for n in targets} and any(
                    kinds.get(t.removeprefix("doc:")) == "aws-api-gateway" for t in targets
                )
                for target in targets:
                    connect(
                        previous_id,
                        target,
                        "SigV4 fan-out" if fan_out else "",
                        dashed=fan_out,
                        colour=_FANOUT_COLOUR if fan_out else _EDGE_COLOUR,
                    )
                if len(targets) != 1:
                    break
                previous_id = targets[0]

    for name in clusters:
        document = documents[name]
        by_service: dict[str, list[str]] = {}
        for rule in document.get("x-gco-routes", []):
            if isinstance(rule, dict):
                by_service.setdefault(str(rule.get("service")), []).append(str(rule.get("prefix")))
        for service, prefixes in by_service.items():
            for prefix in prefixes:
                connect(f"doc:{name}", f"doc:{service}", prefix)
        for service, info in not_exposed.items():
            connect(
                f"doc:{info.get('via')}",
                f"doc:{service}",
                f"{info.get('pathPrefix')}*",
                dashed=True,
            )
        for service, detail in _dict(document.get("x-gco-downstream")).items():
            info = _dict(detail)
            connect(
                f"doc:{service}",
                f"hop:{info.get('hop') or service}",
                "by endpoint name",
                dashed=True,
            )

    return list(nodes.values()), list(edges.values())


def _label_lines(labels: list[str]) -> list[str]:
    """Edge labels as lines: paths under one deep prefix collapse to ``prefix*``."""
    if len(labels) > 1 and all(label.startswith("/") for label in labels):
        import os.path

        common = os.path.commonprefix(labels)
        prefix = common[: common.rfind("/") + 1]
        if prefix.count("/") >= 3:
            return [f"{prefix}*"]
    return labels


def _layout(nodes: list[_Node]) -> tuple[int, int]:
    """Place nodes on the column grid, columns vertically centred; return the canvas size."""
    columns: dict[int, list[_Node]] = {}
    for node in nodes:
        columns.setdefault(node.column, []).append(node)
    heights = {
        column: sum(n.height for n in members) + _NODE_GAP * (len(members) - 1)
        for column, members in columns.items()
    }
    total_height = max(heights.values()) if heights else 0
    x = _MARGIN
    for column, width in enumerate(_COLUMN_WIDTHS):
        y = _MARGIN + (total_height - heights.get(column, 0)) // 2
        for node in columns.get(column, []):
            node.x, node.y = x, y
            y += node.height + _NODE_GAP
        x += width + _COLUMN_GAPS[column]
    return x + _MARGIN, total_height + 2 * _MARGIN


def _svg_text(
    x: float,
    y: float,
    text: str,
    *,
    size: float,
    mono: bool = False,
    weight: str = "normal",
    anchor: str = "start",
    fill: str = "#0f172a",
) -> str:
    font = _SVG_MONO if mono else _SVG_FONT
    return (
        f'<text x="{x:g}" y="{y:g}" font-family="{font}" font-size="{size:g}" '
        f'font-weight="{weight}" text-anchor="{anchor}" fill="{fill}">{_xml(text)}</text>'
    )


def _xml(text: str) -> str:
    """Escape what would end or alter an SVG text node or attribute value."""
    return (
        text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")
    )


def _svg_node(node: _Node) -> str:
    fill, stroke = _NODE_STYLES[node.style]
    dash = ' stroke-dasharray="6 4"' if node.style == "dynamic" else ""
    parts = [
        f'<rect x="{node.x}" y="{node.y}" width="{node.width}" height="{node.height}" rx="8" '
        f'fill="{fill}" stroke="{stroke}" stroke-width="1.5"{dash}/>',
        _svg_text(node.x + 10, node.y + 19, node.title, size=12.5, weight="bold", fill=stroke),
    ]
    for index, line in enumerate(node.lines):
        parts.append(
            _svg_text(
                node.x + 10,
                node.y + _TITLE_HEIGHT + 8 + _LINE_HEIGHT * index,
                line,
                size=10.5,
                mono=True,
                fill="#1e293b",
            )
        )
    body = "\n    ".join(parts)
    if node.href:
        return f'<a href="{_xml(node.href)}"><title>{_xml(node.title)}</title>\n    {body}\n  </a>'
    return body


def _svg_edge(edge: _Edge, nodes: dict[str, _Node]) -> str:
    source, target = nodes[edge.source], nodes[edge.target]
    ty = target.y + target.height / 2
    forward = target.column > source.column
    if forward:
        # Right edge of the source, mid-height, to the left edge of the target.
        sx, sy, tx = source.x + source.width, source.y + source.height / 2, target.x
        mx = sx + 24
    else:
        # A hop back to an earlier column leaves from the source's lower left
        # so it never overprints the edge arriving at the source's midpoint.
        sx, sy, tx = source.x, source.y + source.height - 12, target.x + target.width
        mx = sx - 24
    path = f"M {sx:g} {sy:g} H {mx:g} V {ty:g} H {tx:g}"
    dash = ' stroke-dasharray="7 5"' if edge.dashed else ""
    marker = "arrow-fanout" if edge.colour == _FANOUT_COLOUR else "arrow"
    parts = [
        f'<path d="{path}" fill="none" stroke="{edge.colour}" stroke-width="1.4"{dash} '
        f'marker-end="url(#{marker})"/>'
    ]
    # Forward labels sit above the last segment, right-aligned at the target and
    # stacked upward one line per label so several prefixes into one Service
    # never overprint; a backward label hangs below its first segment.
    lx, base_y, step = (tx - 8, ty - 6, -12) if forward else (sx - 8, sy + 14, 12)
    for index, label in enumerate(reversed(_label_lines(edge.labels))):
        parts.append(
            _svg_text(
                lx,
                base_y + step * index,
                label,
                size=9.5,
                mono=True,
                anchor="end",
                fill=edge.colour,
            )
        )
    return "\n  ".join(parts)


def render_topology(documents: dict[str, dict[str, Any]], *, site_url: str) -> str:
    """The interaction diagram as a standalone SVG document."""
    nodes, edges = topology_model(documents, site_url=site_url)
    by_id = {node.id: node for node in nodes}
    width, height = _layout(nodes)
    legend_y = height + 4
    legend_parts = []
    x = _MARGIN
    for style, label in _LEGEND:
        fill, stroke = _NODE_STYLES[style]
        dash = ' stroke-dasharray="4 3"' if style == "dynamic" else ""
        legend_parts.append(
            f'<rect x="{x}" y="{legend_y}" width="16" height="12" rx="3" fill="{fill}" '
            f'stroke="{stroke}" stroke-width="1.2"{dash}/>'
        )
        legend_parts.append(_svg_text(x + 22, legend_y + 10, label, size=10.5, fill="#334155"))
        x += 22 + int(len(label) * 6.0) + 26
    legend_parts.append(
        _svg_text(
            _MARGIN,
            legend_y + 30,
            "† route exists only under a deployment condition (see the sheet) · dashed arrow: "
            "opt-in or dynamic path · every box links to its spec sheet",
            size=10.5,
            fill="#334155",
        )
    )
    legend_parts.append(
        _svg_text(
            _MARGIN,
            legend_y + 48,
            f"Generated by diagrams/api_specs/generate.py from docs/openapi/*.json; "
            f"regenerate with `{REGENERATION_COMMAND}`.",
            size=9.5,
            fill="#64748b",
        )
    )
    total_height = legend_y + 60
    lines = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        # The default SVG namespace only: links use SVG 2's plain ``href``, and
        # .github/scripts/validate_svg_assets.py refuses ``xlink`` (or any other
        # foreign namespace) in a tracked SVG.
        f'<svg xmlns="http://www.w3.org/2000/svg" '
        f'width="{width}" height="{total_height}" viewBox="0 0 {width} {total_height}" '
        'role="img" aria-labelledby="title desc">',
        '  <title id="title">GCO API topology: API Gateways, Lambda proxies, the cluster Gateway and the services</title>',
        '  <desc id="desc">Clients sign requests with SigV4 to the global or regional API Gateway; Lambda '
        "proxies add the HMAC envelope and forward to the region's internal ALB, the Kubernetes "
        "Gateway whose HTTPRoute routes by path prefix to the FastAPI services.</desc>",
        "  <defs>",
        f'    <marker id="arrow" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="8" markerHeight="8" orient="auto-start-reverse"><path d="M 0 0 L 10 5 L 0 10 z" fill="{_EDGE_COLOUR}"/></marker>',
        f'    <marker id="arrow-fanout" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="8" markerHeight="8" orient="auto-start-reverse"><path d="M 0 0 L 10 5 L 0 10 z" fill="{_FANOUT_COLOUR}"/></marker>',
        "  </defs>",
        f'  <rect width="{width}" height="{total_height}" fill="#ffffff"/>',
    ]
    lines.extend(f"  {_svg_edge(edge, by_id)}" for edge in edges)
    lines.extend(f"  {_svg_node(node)}" for node in nodes)
    lines.extend(f"  {part}" for part in legend_parts)
    lines.append("</svg>")
    return "\n".join(lines) + "\n"


# ─── the catalogue contract ──────────────────────────────────────────────────


def expected_outputs(project_root: Path) -> dict[Path, str]:
    """``{path: content}`` for every file this catalogue owns, rendered in memory."""
    openapi_dir = project_root / "docs" / "openapi"
    output_dir = project_root / "diagrams" / "api_specs"
    site_url, repo_url = read_site_config(project_root / "mkdocs.yml")
    documents = {
        service: load_document(service, openapi_dir) for service in service_names(openapi_dir)
    }
    outputs = {
        output_dir / f"{service}.md": render_sheet(
            service, document, site_url=site_url, repo_url=repo_url
        )
        for service, document in documents.items()
    }
    outputs[output_dir / "README.md"] = render_index(
        documents, site_url=site_url, repo_url=repo_url
    )
    outputs[output_dir / TOPOLOGY_FILE] = render_topology(documents, site_url=site_url)
    return outputs


def api_contract_issues(project_root: Path) -> list[str]:
    """Stale, missing and orphan sheets, by repository path; empty when current.

    A checkout that cannot be rendered at all (no OpenAPI documents, no
    ``mkdocs.yml``, a colliding anchor) is reported as an issue rather than
    raised, so the aggregate ``--check`` prints one actionable line per
    catalogue instead of a traceback.
    """
    output_dir = project_root / "diagrams" / "api_specs"
    try:
        expected = expected_outputs(project_root)
    except (SpecSheetError, OSError) as exc:
        return [f"API spec sheets cannot be rendered: {exc}"]
    issues: list[str] = []
    for path, content in sorted(expected.items()):
        relative = path.relative_to(project_root).as_posix()
        if not path.is_file():
            issues.append(f"missing API spec sheet: {relative}")
        elif path.read_text(encoding="utf-8") != content:
            issues.append(f"stale API spec sheet: {relative}")
    for path in sorted([*output_dir.glob("*.md"), *output_dir.glob("*.svg")]):
        if path not in expected:
            issues.append(f"orphan API spec sheet: {path.relative_to(project_root).as_posix()}")
    if issues:
        issues.append(f"regenerate with `{REGENERATION_COMMAND}`")
    return issues


def write_outputs(project_root: Path) -> list[str]:
    """Write every owned file; return the repository paths that changed."""
    changed: list[str] = []
    for path, content in sorted(expected_outputs(project_root).items()):
        current = path.read_text(encoding="utf-8") if path.is_file() else None
        if current != content:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")
            changed.append(path.relative_to(project_root).as_posix())
    return changed


# ─── Swagger UI consoles ─────────────────────────────────────────────────────


def _console_index(services: dict[str, str], site_url: str) -> str:
    items = "\n".join(
        f'      <li><a href="./{service}/">{title}</a> <code>{service}</code></li>'
        for service, title in sorted(services.items())
    )
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>GCO API consoles</title>
  <link rel="icon" href="./assets/favicon-32x32.png">
  <style>
    body {{ font-family: system-ui, sans-serif; margin: 2rem auto; max-width: 42rem; line-height: 1.5; }}
    code {{ background: #f2f2f2; padding: 0 .3em; border-radius: 3px; }}
  </style>
</head>
<body>
  <h1>GCO API consoles</h1>
  <p>FastAPI's Swagger UI for each HTTP service, rendered from the committed OpenAPI
     documents. Read-only: these pages describe the API and do not send requests.</p>
  <ul>
{items}
  </ul>
  <p><a href="{site_url}">Back to the wiki</a></p>
</body>
</html>
"""


def build_swagger_site(site_dir: Path, assets_dir: Path, *, project_root: Path) -> list[Path]:
    """Write FastAPI's Swagger UI console per service under ``site_dir``.

    ``assets_dir`` is the ``swagger-ui-dist`` package directory; its bundle,
    stylesheet and favicon (plus LICENSE/NOTICE when shipped) are copied to
    ``site_dir/assets`` so the console loads everything from the site itself.
    Every generated page is checked to reference no remote resource, so a
    FastAPI default creeping back in fails the build instead of the site's
    zero-third-party-requests rule. Returns the written files.
    """
    from fastapi.openapi.docs import get_swagger_ui_html

    missing = [name for name in SWAGGER_ASSET_FILES if not (assets_dir / name).is_file()]
    if missing:
        raise SpecSheetError(
            f"swagger-ui-dist assets missing from {assets_dir}: {', '.join(missing)} "
            "(run `npm ci --ignore-scripts --no-audit --no-fund`)"
        )
    site_url, _repo_url = read_site_config(project_root / "mkdocs.yml")
    openapi_dir = project_root / "docs" / "openapi"
    written: list[Path] = []
    assets_out = site_dir / "assets"
    assets_out.mkdir(parents=True, exist_ok=True)
    for name in (*SWAGGER_ASSET_FILES, *SWAGGER_NOTICE_FILES):
        source = assets_dir / name
        if source.is_file():
            written.append(Path(shutil.copyfile(source, assets_out / name)))

    titles: dict[str, str] = {}
    for service in service_names(openapi_dir):
        document = load_document(service, openapi_dir)
        titles[service] = str(_dict(document.get("info")).get("title") or service)
        console_dir = site_dir / service
        console_dir.mkdir(parents=True, exist_ok=True)
        spec_copy = console_dir / "openapi.json"
        spec_copy.write_text(
            (openapi_dir / f"{service}.json").read_text(encoding="utf-8"), encoding="utf-8"
        )
        written.append(spec_copy)
        page = get_swagger_ui_html(
            openapi_url="openapi.json",
            title=f"{titles[service]} — Swagger UI",
            swagger_js_url="../assets/swagger-ui-bundle.js",
            swagger_css_url="../assets/swagger-ui.css",
            swagger_favicon_url="../assets/favicon-32x32.png",
            # No validator badge (a request to validator.swagger.io) and no
            # "Try it out": the site has no backend to send requests to.
            swagger_ui_parameters={"validatorUrl": None, "supportedSubmitMethods": []},
        )
        html = bytes(page.body).decode("utf-8")
        if re.search(r"(?:src|href)=\"https?://", html):
            raise SpecSheetError(f"{service}: the Swagger UI page references a remote resource")
        # FastAPI serves this page with a ``charset=utf-8`` header and so omits
        # the meta tag; a static host may not. Without it a browser falls back
        # to Latin-1 for the page *and* the bundle it loads, and the bundle's
        # non-ASCII literals then fail to parse ("SwaggerUIBundle is not defined").
        html = html.replace("<head>", '<head>\n    <meta charset="utf-8">', 1)
        if '<meta charset="utf-8">' not in html:
            raise SpecSheetError(f"{service}: FastAPI's Swagger UI template has no <head> to tag")
        index = console_dir / "index.html"
        index.write_text(html, encoding="utf-8")
        written.append(index)
    root_index = site_dir / "index.html"
    root_index.write_text(_console_index(titles, site_url), encoding="utf-8")
    written.append(root_index)
    return written


# ─── CLI ─────────────────────────────────────────────────────────────────────


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--check",
        action="store_true",
        help="Do not write; exit non-zero if any committed sheet is stale.",
    )
    parser.add_argument(
        "--swagger-ui-dir",
        type=Path,
        help="Also build the Swagger UI consoles into this directory (the Pages /swagger/ tree).",
    )
    parser.add_argument(
        "--swagger-assets",
        type=Path,
        default=None,
        help="The swagger-ui-dist package directory to self-host from "
        "(default: node_modules/swagger-ui-dist).",
    )
    args = parser.parse_args(argv)
    swagger_assets = args.swagger_assets or REPO_ROOT / "node_modules" / "swagger-ui-dist"

    try:
        if args.check:
            issues = api_contract_issues(REPO_ROOT)
            for issue in issues:
                print(f"ERROR: {issue}", file=sys.stderr)
            if issues:
                return 1
            print("API spec sheets are current")
        else:
            changed = write_outputs(REPO_ROOT)
            for path in changed:
                print(f"{path}: written")
            if not changed:
                print("API spec sheets unchanged")
        if args.swagger_ui_dir is not None:
            written = build_swagger_site(
                args.swagger_ui_dir, swagger_assets, project_root=REPO_ROOT
            )
            print(f"Swagger UI consoles: {len(written)} files under {args.swagger_ui_dir}")
    except SpecSheetError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
