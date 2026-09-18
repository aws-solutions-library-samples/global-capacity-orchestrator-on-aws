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
    if facts:
        lines.extend([*facts, ""])

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

    lines = [f"# {title} — API spec sheet", "", banner(service), ""]
    lines.append(
        f"*Service {code(service)} · OpenAPI {document.get('openapi', '?')} · "
        f"API version {info.get('version', '?')} · {len(operations)} endpoints · {len(schemas)} schemas*"
    )
    lines.append("")
    lines.extend(_prose(info.get("description")))
    if lines[-1] != "":
        lines.append("")
    lines.extend(
        [
            f"- **Machine-readable document:** [{code(f'docs/openapi/{service}.json')}]"
            f"({repo_url}/blob/main/docs/openapi/{service}.json) — the FastAPI `app.openapi()` "
            "export this sheet is rendered from",
            f"- **Interactive console (Swagger UI):** <{site_url}{SWAGGER_SITE_PREFIX}/{service}/>",
            "- **Catalogue index:** [README.md](README.md)",
            "",
            "## Endpoints",
            "",
        ]
    )
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
        lines.extend(["This service declares no component schemas.", ""])
    return "\n".join(lines).rstrip() + "\n"


def render_index(documents: dict[str, dict[str, Any]], *, site_url: str, repo_url: str) -> str:
    rows = []
    for service, document in sorted(documents.items()):
        info = _dict(document.get("info"))
        rows.append(
            [
                f"[{code(service)}]({service}.md)",
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
        "One spec sheet per GCO HTTP service, rendered from the OpenAPI document FastAPI",
        "generates for it — the same document behind the service's automatic Swagger",
        "`/docs` page. The documents themselves are committed under `docs/openapi/` and",
        "checked against the live routes by `tests/test_api_docs_coverage.py`; these",
        "sheets are a deterministic rendering of them, so a route, parameter or model",
        "change shows up here in the same pull request as the code.",
        "",
        *table(
            ["Service", "API", "Version", "Endpoints", "OpenAPI document", "Swagger UI"],
            rows,
        ),
        "",
        "The **Swagger UI** column is FastAPI's own `/docs` console for each service,",
        f"served from the project site under `/{SWAGGER_SITE_PREFIX}/` with a self-hosted copy of",
        "`swagger-ui-dist` (the site makes no third-party requests). It is built at deploy",
        "time by `pages.yml`; the sheets in this directory are also injected into the",
        "wiki under `/api/` by `scripts/mkdocs_hooks.py`, so both renderings come from",
        "one source.",
        "",
        "## Regenerating",
        "",
        "```bash",
        "python scripts/generate_openapi.py        # refresh docs/openapi/*.json from the apps",
        f"{REGENERATION_COMMAND}                     # rewrite these sheets and this index",
        "python diagrams/generate.py --check       # fail if anything here is stale",
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
    for path in sorted(output_dir.glob("*.md")):
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
