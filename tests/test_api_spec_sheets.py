"""``diagrams/api_specs/generate.py`` — the API spec sheets and Swagger consoles.

The sheets are a deterministic rendering of the committed OpenAPI documents,
so two things are pinned here: the rendering itself on synthetic documents
(every JSON Schema shape the renderer distinguishes, the Markdown it must emit
to stay lint-clean and linkable), and the catalogue contract against the real
repository — the committed sheets equal a fresh render, the generated banner
is present, the services match ``scripts/generate_openapi.py``, and every link
target the sheets use exists. The Swagger tree is built against a stand-in
``swagger-ui-dist`` so the assertions (local assets only, ``charset``, copied
notices, refusal of a remote resource) never need npm.
"""

from __future__ import annotations

import importlib.util
import json
import re
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

from diagrams.api_specs import generate as sheets

REPO_ROOT = Path(__file__).resolve().parent.parent

MKDOCS_YML = "site_url: https://example.test/site/\nrepo_url: https://example.test/org/repo\n"


# ─── fixtures ────────────────────────────────────────────────────────────────


def _document(**overrides: Any) -> dict[str, Any]:
    document: dict[str, Any] = {
        "openapi": "3.1.0",
        "info": {
            "title": "Widget API",
            "version": "1.2.3",
            "description": "Widgets.\n\nTwo lines.",
        },
        "paths": {
            "/": {
                "get": {
                    "summary": "Root",
                    "operationId": "root",
                    "responses": {"200": {"description": "OK"}},
                }
            },
            "/widgets/{id}": {
                "delete": {
                    "summary": "Delete Widget",
                    "description": "Remove | destroy a widget.",
                    "operationId": "delete_widget",
                    "tags": ["Widgets"],
                    "deprecated": True,
                    "parameters": [
                        {
                            "name": "id",
                            "in": "path",
                            "required": True,
                            "schema": {"type": "string", "pattern": "^w-[0-9]+$", "title": "Id"},
                        },
                        "not-a-parameter",
                    ],
                    "responses": {
                        "204": {"description": "Deleted"},
                        "404": "not-a-response",
                        "422": {
                            "description": "Validation Error",
                            "content": {
                                "application/json": {
                                    "schema": {"$ref": "#/components/schemas/Problem"}
                                }
                            },
                        },
                    },
                },
                "get": {
                    "description": "Fetch one widget.",
                    "operationId": "get_widget",
                    "tags": ["Widgets"],
                    "parameters": [
                        {
                            "name": "id",
                            "in": "path",
                            "required": True,
                            "description": "Widget id",
                            "schema": {"type": "string"},
                        },
                        {
                            "name": "verbose",
                            "in": "query",
                            "schema": {
                                "anyOf": [{"type": "boolean"}, {"type": "null"}],
                                "default": False,
                                "description": "Include | details",
                            },
                        },
                    ],
                    "responses": {
                        "200": {
                            "description": "The widget",
                            "content": {
                                "application/json": {
                                    "schema": {"$ref": "#/components/schemas/Widget"}
                                },
                                "text/plain": {"schema": {"type": "string"}},
                            },
                        }
                    },
                },
                "post": {
                    "operationId": "replace_widget",
                    "tags": "not-a-list",
                    "requestBody": {
                        "required": True,
                        "content": {
                            "application/json": {"schema": {"$ref": "#/components/schemas/Widget"}}
                        },
                    },
                    "responses": {
                        "200": {
                            "description": "Replaced",
                            "content": {"application/json": {"schema": {}}},
                        }
                    },
                },
                "trace": "not-an-operation",
            },
            "/bulk": {
                "post": {
                    "summary": "Bulk",
                    "requestBody": {
                        "content": {
                            "application/json": {
                                "schema": {"type": "array", "items": {"type": "string"}}
                            }
                        }
                    },
                    "responses": {},
                }
            },
            "/broken": "not-a-path-item",
        },
        "components": {
            "schemas": {
                "Widget": {
                    "title": "Widget",
                    "description": "A widget.",
                    "type": "object",
                    "required": ["name"],
                    "properties": {
                        "name": {
                            "type": "string",
                            "minLength": 1,
                            "maxLength": 64,
                            "description": "Display | name",
                        },
                        "size": {
                            "type": "integer",
                            "minimum": 0,
                            "exclusiveMaximum": 100,
                            "default": 1,
                        },
                        "tags": {
                            "type": "array",
                            "items": {"type": "string"},
                            "minItems": 0,
                            "maxItems": 5,
                        },
                        "owner": {
                            "anyOf": [{"$ref": "#/components/schemas/Owner"}, {"type": "null"}]
                        },
                        "labels": {"type": "object", "additionalProperties": {"type": "string"}},
                        "extra": {"type": "object", "additionalProperties": True},
                        "blob": {"additionalProperties": {}},
                        "nothing": {},
                        "either": {"oneOf": [{"type": "string"}, {"type": "integer"}]},
                        "both": {
                            "allOf": [
                                {"type": "object"},
                                {"type": "object", "properties": {"x": {}}},
                            ]
                        },
                        "only_null": {"anyOf": [{"type": "null"}]},
                        "kind": {"const": "widget"},
                        "when": {"type": "string", "format": "date-time"},
                        "multi": {"type": ["string", "integer"], "multipleOf": 2},
                        "nested": {
                            "type": "object",
                            "additionalProperties": {},
                            "properties": {"y": {}},
                        },
                        "malformed": "not-a-schema",
                    },
                    "example": {"name": "w"},
                },
                "Owner": {"type": "string", "enum": ["alice", "bob"]},
                "Problem": {"type": "object", "properties": {"detail": {"type": "string"}}},
                "Opaque": "not-a-schema",
            }
        },
    }
    document.update(overrides)
    return document


def _repo(tmp_path: Path, documents: dict[str, dict[str, Any]] | None = None) -> Path:
    """A stand-in checkout: docs/openapi/*.json, mkdocs.yml, an empty catalogue."""
    root = tmp_path / "repo"
    (root / "docs" / "openapi").mkdir(parents=True)
    (root / "diagrams" / "api_specs").mkdir(parents=True)
    (root / "mkdocs.yml").write_text(MKDOCS_YML, encoding="utf-8")
    for service, document in (documents or {"widget-service": _document()}).items():
        (root / "docs" / "openapi" / f"{service}.json").write_text(
            json.dumps(document, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
    return root


def _rendered(tmp_path: Path) -> str:
    root = _repo(tmp_path)
    return sheets.expected_outputs(root)[root / "diagrams" / "api_specs" / "widget-service.md"]


# ─── inputs ──────────────────────────────────────────────────────────────────


def test_service_names_are_the_document_stems_sorted(tmp_path: Path) -> None:
    root = _repo(tmp_path, {"zeta": _document(), "alpha": _document()})
    assert sheets.service_names(root / "docs" / "openapi") == ["alpha", "zeta"]


def test_no_documents_is_an_error(tmp_path: Path) -> None:
    (tmp_path / "empty").mkdir()
    with pytest.raises(sheets.SpecSheetError, match="no OpenAPI documents"):
        sheets.service_names(tmp_path / "empty")


@pytest.mark.parametrize("payload", ["[]", '{"info": {}}', '{"paths": []}'])
def test_documents_without_a_paths_object_are_refused(tmp_path: Path, payload: str) -> None:
    (tmp_path / "svc.json").write_text(payload, encoding="utf-8")
    with pytest.raises(sheets.SpecSheetError, match="no paths object"):
        sheets.load_document("svc", tmp_path)


def test_site_config_comes_from_mkdocs_yml(tmp_path: Path) -> None:
    path = tmp_path / "mkdocs.yml"
    path.write_text("x: 1\nsite_url: https://h/site\nrepo_url: https://g/o/r/\n", encoding="utf-8")
    assert sheets.read_site_config(path) == ("https://h/site/", "https://g/o/r")


def test_site_config_requires_both_urls(tmp_path: Path) -> None:
    path = tmp_path / "mkdocs.yml"
    path.write_text("site_url: https://h/site/\n", encoding="utf-8")
    with pytest.raises(sheets.SpecSheetError, match="site_url and repo_url"):
        sheets.read_site_config(path)


# ─── Markdown primitives ─────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("heading", "slug"),
    [
        ("`GET /api/v1/jobs/{namespace}/{name}`", "get-apiv1jobsnamespacename"),
        ("HTTPValidationError", "httpvalidationerror"),
        ("  Two   words ", "two-words"),
    ],
)
def test_slugify_matches_the_renderers(heading: str, slug: str) -> None:
    assert sheets.slugify(heading) == slug


def test_cell_collapses_whitespace_escapes_pipes_and_dashes_empties() -> None:
    assert sheets.cell("a |\n  b") == "a \\| b"
    assert sheets.cell(None) == "—"
    assert sheets.cell("   ") == "—"
    assert sheets.cell(7) == "7"


def test_table_pads_and_truncates_rows_to_the_header_width() -> None:
    lines = sheets.table(["A", "B"], [["1"], ["1", "2", "3"]])
    assert lines == ["| A | B |", "|---|---|", "| 1 | — |", "| 1 | 2 |"]


# ─── schema rendering ────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("schema", "rendered"),
    [
        ("not-a-dict", "any"),
        ({}, "any"),
        ({"$ref": "#/components/schemas/Widget"}, "[`Widget`](#schema-widget)"),
        ({"anyOf": [{"type": "string"}, {"type": "null"}]}, "string (nullable)"),
        ({"anyOf": [{"type": "null"}]}, "null (nullable)"),
        ({"oneOf": [{"type": "string"}, {"type": "integer"}]}, "string or integer"),
        (
            {"allOf": [{"type": "object"}, {"$ref": "#/components/schemas/W"}]},
            "object and [`W`](#schema-w)",
        ),
        ({"anyOf": []}, "any"),
        ({"enum": ["a", 1, None]}, 'enum: `"a"`, `1`, `null`'),
        ({"const": "x"}, 'const `"x"`'),
        ({"additionalProperties": {}}, "object (free-form)"),
        ({"properties": {"a": {}}}, "object (free-form)"),
        ({"type": "array", "items": {"type": "integer"}}, "array of integer"),
        ({"type": "array"}, "array of any"),
        ({"type": "object", "additionalProperties": {"type": "string"}}, "object of string"),
        ({"type": "object", "additionalProperties": True}, "object (free-form)"),
        ({"type": "object", "additionalProperties": {}}, "object (free-form)"),
        ({"type": "object", "additionalProperties": {}, "properties": {"y": {}}}, "object"),
        ({"type": "object", "properties": {"y": {}}}, "object"),
        ({"type": "object"}, "object"),
        ({"type": "string", "format": "date-time"}, "string (date-time)"),
        ({"type": "string"}, "string"),
        ({"type": ["string", "integer"]}, "string or integer"),
        ({"type": "boolean"}, "boolean"),
    ],
)
def test_schema_type_names_every_shape(schema: Any, rendered: str) -> None:
    assert sheets.schema_type(schema) == rendered


def test_references_outside_component_schemas_are_refused() -> None:
    with pytest.raises(sheets.SpecSheetError, match="unsupported \\$ref"):
        sheets.schema_type({"$ref": "#/components/responses/Nope"})


def test_constraints_collect_bounds_including_nullable_variants() -> None:
    schema = {
        "anyOf": [{"type": "string", "maxLength": 10, "pattern": "^a$"}, {"type": "null"}],
        "minLength": 1,
    }
    assert sheets.constraints(schema) == "min length 1; max length 10; pattern `^a$`"
    assert (
        sheets.constraints({"minimum": 1, "exclusiveMaximum": 9, "multipleOf": 3})
        == "≥ 1; < 9; multiple of 3"
    )
    assert (
        sheets.constraints({"minItems": 0, "maxItems": 2, "exclusiveMinimum": 0})
        == "> 0; min items 0; max items 2"
    )
    assert sheets.constraints({}) == "—"
    assert sheets.constraints("no") == "—"
    # A bound repeated across variants is reported once.
    assert sheets.constraints({"anyOf": [{"minimum": 1}, {"minimum": 1}]}) == "≥ 1"


def test_default_of_quotes_json_defaults() -> None:
    assert sheets.default_of({"default": "createdAt:desc"}) == '`"createdAt:desc"`'
    assert sheets.default_of({"default": None}) == "`null`"
    assert sheets.default_of({}) == "—"
    assert sheets.default_of(None) == "—"


# ─── the sheet ───────────────────────────────────────────────────────────────


def test_sheet_header_banner_and_links(tmp_path: Path) -> None:
    text = _rendered(tmp_path)
    lines = text.splitlines()
    assert lines[0] == "# Widget API — API spec sheet"
    assert lines[2] == sheets.banner("widget-service")
    assert "Do not edit by hand" in lines[2]
    assert (
        lines[4]
        == "*Service `widget-service` · OpenAPI 3.1.0 · API version 1.2.3 · 5 endpoints · 4 schemas*"
    )
    assert "Widgets." in lines and "Two lines." in lines
    assert (
        "[`docs/openapi/widget-service.json`](https://example.test/org/repo/blob/main/docs/openapi/widget-service.json)"
        in text
    )
    assert "<https://example.test/site/swagger/widget-service/>" in text
    assert "- **Catalogue index:** [README.md](README.md)" in lines
    assert text.endswith("\n") and not text.endswith("\n\n")


def test_endpoint_table_lists_operations_in_reading_order_with_anchors(tmp_path: Path) -> None:
    text = _rendered(tmp_path)
    table_start = text.index("## Endpoints")
    table_end = text.index("## Endpoint details")
    rows = [line for line in text[table_start:table_end].splitlines() if line.startswith("| [")]
    # Paths in document order (the committed documents are key-sorted), methods
    # in reading order rather than alphabetically.
    assert rows == [
        "| [`GET`](#op-get) | `/` | Root | — |",
        "| [`POST`](#op-post-bulk) | `/bulk` | Bulk | — |",
        "| [`GET`](#op-get-widgets-id) | `/widgets/{id}` | — | Widgets |",
        "| [`POST`](#op-post-widgets-id) | `/widgets/{id}` | — | — |",
        "| [`DELETE`](#op-delete-widgets-id) | `/widgets/{id}` | Delete Widget | Widgets |",
    ]
    # Every anchor the table links to is declared exactly once before its heading.
    for anchor in (
        "op-get",
        "op-get-widgets-id",
        "op-post-widgets-id",
        "op-delete-widgets-id",
        "op-post-bulk",
    ):
        assert text.count(f'<a id="{anchor}"></a>') == 1


def test_operation_sections_render_facts_parameters_bodies_and_responses(tmp_path: Path) -> None:
    text = _rendered(tmp_path)
    delete = text[text.index('<a id="op-delete-widgets-id"></a>') : text.index("## Schemas")]
    assert "### `DELETE /widgets/{id}`" in delete
    assert "Remove | destroy a widget." in delete, "prose keeps its pipes; only cells escape them"
    assert "- **Operation ID:** `delete_widget`" in delete
    assert "- **Tags:** Widgets" in delete
    assert "- **Deprecated:** yes" in delete
    assert "| `id` | path | string | yes | — | pattern `^w-[0-9]+$` | — |" in delete
    assert "| `204` | Deleted | — |" in delete
    assert "| `404` | — | — |" in delete, "a malformed response still gets a row"
    assert (
        "| `422` | Validation Error | `application/json`: [`Problem`](#schema-problem) |" in delete
    )

    get = text[
        text.index('<a id="op-get-widgets-id"></a>') : text.index('<a id="op-post-widgets-id"></a>')
    ]
    assert "Fetch one widget." in get
    assert (
        "| `verbose` | query | boolean (nullable) | no | `false` | — | Include \\| details |" in get
    )
    assert "| `id` | path | string | yes | — | — | Widget id |" in get
    assert "`application/json`: [`Widget`](#schema-widget); `text/plain`: string" in get

    post = text[
        text.index('<a id="op-post-widgets-id"></a>') : text.index(
            '<a id="op-delete-widgets-id"></a>'
        )
    ]
    assert "**Request body** (required): `application/json`: [`Widget`](#schema-widget)" in post
    assert "- **Tags:**" not in post, "a malformed tags value is ignored"
    assert "| `200` | Replaced | `application/json`: any |" in post

    bulk = text[
        text.index('<a id="op-post-bulk"></a>') : text.index('<a id="op-get-widgets-id"></a>')
    ]
    assert "**Request body** (optional): `application/json`: array of string" in bulk
    assert "**Responses**" not in bulk, "an empty responses object renders no table"
    assert "- **Operation ID:**" not in bulk

    root = text[text.index('<a id="op-get"></a>') : text.index('<a id="op-post-bulk"></a>')]
    assert "\nRoot\n" in root, "the summary stands in for a missing description"


def test_schema_sections_render_property_tables_examples_and_enums(tmp_path: Path) -> None:
    text = _rendered(tmp_path)
    # Components arrive key-sorted from the committed document: Opaque, Owner, Problem, Widget.
    widget = text[text.index('<a id="schema-widget"></a>') :]
    assert "### `Widget`" in widget and "A widget." in widget
    expected_rows = {
        "| `name` | string | yes | — | min length 1; max length 64 | Display \\| name |",
        "| `size` | integer | no | `1` | ≥ 0; < 100 | — |",
        "| `tags` | array of string | no | — | min items 0; max items 5 | — |",
        "| `owner` | [`Owner`](#schema-owner) (nullable) | no | — | — | — |",
        "| `labels` | object of string | no | — | — | — |",
        "| `extra` | object (free-form) | no | — | — | — |",
        "| `blob` | object (free-form) | no | — | — | — |",
        "| `nothing` | any | no | — | — | — |",
        "| `either` | string or integer | no | — | — | — |",
        "| `both` | object and object | no | — | — | — |",
        "| `only_null` | null (nullable) | no | — | — | — |",
        '| `kind` | const `"widget"` | no | — | — | — |',
        "| `when` | string (date-time) | no | — | — | — |",
        "| `multi` | string or integer | no | — | multiple of 2 | — |",
        "| `nested` | object | no | — | — | — |",
        "| `malformed` | any | no | — | — | — |",
    }
    assert expected_rows <= set(widget.splitlines())
    assert 'Example:\n\n```json\n{\n  "name": "w"\n}\n```' in widget

    owner = text[
        text.index('<a id="schema-owner"></a>') : text.index('<a id="schema-problem"></a>')
    ]
    assert '- **Type:** enum: `"alice"`, `"bob"`' in owner, (
        "a schema without properties states its type"
    )

    opaque = text[
        text.index('<a id="schema-opaque"></a>') : text.index('<a id="schema-owner"></a>')
    ]
    assert "- **Type:** any" in opaque, "a malformed component schema still gets a section"


def test_a_document_without_schemas_says_so(tmp_path: Path) -> None:
    document = _document(components={})
    del document["info"]
    root = _repo(tmp_path, {"bare": document})
    text = sheets.expected_outputs(root)[root / "diagrams" / "api_specs" / "bare.md"]
    assert text.startswith("# bare — API spec sheet")
    assert "API version ? · 5 endpoints · 0 schemas" in text
    assert "This service declares no component schemas." in text


def test_colliding_anchors_are_refused(tmp_path: Path) -> None:
    document = _document()
    document["paths"]["/widgets/id"] = {"get": {"responses": {}}}
    root = _repo(tmp_path, {"clash": document})
    with pytest.raises(sheets.SpecSheetError, match="share the anchor #op-get-widgets-id"):
        sheets.expected_outputs(root)


# ─── the index ───────────────────────────────────────────────────────────────


def test_index_lists_every_service_with_its_three_renderings(tmp_path: Path) -> None:
    root = _repo(
        tmp_path, {"b-svc": _document(), "a-svc": _document(info={"title": "A", "version": "9"})}
    )
    text = sheets.expected_outputs(root)[root / "diagrams" / "api_specs" / "README.md"]
    assert text.startswith(
        "# GCO API Spec Sheets\n\n<!-- Generated by diagrams/api_specs/generate.py"
    )
    rows = [line for line in text.splitlines() if line.startswith("| [")]
    assert rows == [
        "| [`a-svc`](a-svc.md) | A | `9` | 5 | [`json`](https://example.test/org/repo/blob/main/docs/openapi/a-svc.json) | [`/docs`](https://example.test/site/swagger/a-svc/) |",
        "| [`b-svc`](b-svc.md) | Widget API | `1.2.3` | 5 | [`json`](https://example.test/org/repo/blob/main/docs/openapi/b-svc.json) | [`/docs`](https://example.test/site/swagger/b-svc/) |",
    ]
    assert sheets.REGENERATION_COMMAND in text
    assert "--swagger-ui-dir /tmp/gco-swagger" in text


# ─── the catalogue contract ──────────────────────────────────────────────────


def test_contract_reports_missing_stale_and_orphan_sheets_then_the_remedy(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    assert sheets.api_contract_issues(root) == [
        "missing API spec sheet: diagrams/api_specs/README.md",
        "missing API spec sheet: diagrams/api_specs/widget-service.md",
        f"regenerate with `{sheets.REGENERATION_COMMAND}`",
    ]
    assert sorted(sheets.write_outputs(root)) == [
        "diagrams/api_specs/README.md",
        "diagrams/api_specs/widget-service.md",
    ]
    assert sheets.api_contract_issues(root) == []
    assert sheets.write_outputs(root) == [], "a second write changes nothing"

    sheet = root / "diagrams" / "api_specs" / "widget-service.md"
    sheet.write_text(sheet.read_text(encoding="utf-8") + "edited\n", encoding="utf-8")
    (root / "diagrams" / "api_specs" / "old-service.md").write_text("# gone\n", encoding="utf-8")
    assert sheets.api_contract_issues(root) == [
        "stale API spec sheet: diagrams/api_specs/widget-service.md",
        "orphan API spec sheet: diagrams/api_specs/old-service.md",
        f"regenerate with `{sheets.REGENERATION_COMMAND}`",
    ]
    assert sheets.write_outputs(root) == ["diagrams/api_specs/widget-service.md"]


# ─── Swagger UI consoles ─────────────────────────────────────────────────────


def _assets(tmp_path: Path, *, notices: bool = True) -> Path:
    assets = tmp_path / "swagger-ui-dist"
    assets.mkdir()
    for name in sheets.SWAGGER_ASSET_FILES:
        (assets / name).write_bytes(f"asset {name}".encode())
    if notices:
        (assets / "LICENSE").write_text("Apache-2.0\n", encoding="utf-8")
    return assets


def test_swagger_site_is_self_contained(tmp_path: Path) -> None:
    root = _repo(tmp_path, {"svc-a": _document(), "svc-b": _document(info={"title": "B"})})
    site = tmp_path / "site" / "swagger"

    written = sheets.build_swagger_site(site, _assets(tmp_path), project_root=root)

    relative = sorted(path.relative_to(site).as_posix() for path in written)
    assert relative == [
        "assets/LICENSE",
        "assets/favicon-32x32.png",
        "assets/swagger-ui-bundle.js",
        "assets/swagger-ui.css",
        "index.html",
        "svc-a/index.html",
        "svc-a/openapi.json",
        "svc-b/index.html",
        "svc-b/openapi.json",
    ]
    page = (site / "svc-a" / "index.html").read_text(encoding="utf-8")
    assert '<meta charset="utf-8">' in page
    assert "<title>Widget API — Swagger UI</title>" in page
    assert (
        'href="../assets/swagger-ui.css"' in page and 'src="../assets/swagger-ui-bundle.js"' in page
    )
    assert "url: 'openapi.json'" in page
    assert '"validatorUrl": null' in page and '"supportedSubmitMethods": []' in page
    assert "http://" not in page and "https://" not in page, (
        "FastAPI's CDN defaults must be overridden"
    )
    assert (site / "svc-a" / "openapi.json").read_text(encoding="utf-8") == (
        root / "docs" / "openapi" / "svc-a.json"
    ).read_text(encoding="utf-8")
    index = (site / "index.html").read_text(encoding="utf-8")
    assert '<a href="./svc-a/">Widget API</a>' in index and '<a href="./svc-b/">B</a>' in index
    assert '<a href="https://example.test/site/">Back to the wiki</a>' in index
    assert re.search(r'(?:src|href)="https?://', index) is None or "example.test/site/" in index


def test_swagger_site_without_shipped_notices_copies_only_the_assets(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    written = sheets.build_swagger_site(
        tmp_path / "out", _assets(tmp_path, notices=False), project_root=root
    )
    assert not any(path.name == "LICENSE" for path in written)


def test_swagger_site_refuses_missing_assets(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    assets = tmp_path / "swagger-ui-dist"
    assets.mkdir()
    (assets / "swagger-ui.css").write_text("", encoding="utf-8")
    with pytest.raises(
        sheets.SpecSheetError, match=r"missing from .*: swagger-ui-bundle\.js, favicon-32x32\.png"
    ):
        sheets.build_swagger_site(tmp_path / "out", assets, project_root=root)


def test_swagger_site_refuses_a_page_that_reaches_a_remote_host(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import fastapi.openapi.docs as docs
    from starlette.responses import HTMLResponse

    root = _repo(tmp_path)
    monkeypatch.setattr(
        docs,
        "get_swagger_ui_html",
        lambda **kwargs: HTMLResponse(
            '<html><head><script src="https://cdn.example/x.js"></script></head></html>'
        ),
    )
    with pytest.raises(sheets.SpecSheetError, match="references a remote resource"):
        sheets.build_swagger_site(tmp_path / "out", _assets(tmp_path), project_root=root)


def test_swagger_site_refuses_a_template_without_a_head(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import fastapi.openapi.docs as docs
    from starlette.responses import HTMLResponse

    root = _repo(tmp_path)
    monkeypatch.setattr(
        docs, "get_swagger_ui_html", lambda **kwargs: HTMLResponse("<html><body></body></html>")
    )
    with pytest.raises(sheets.SpecSheetError, match="no <head> to tag"):
        sheets.build_swagger_site(tmp_path / "out", _assets(tmp_path), project_root=root)


# ─── CLI ─────────────────────────────────────────────────────────────────────


def test_main_check_reports_and_write_repairs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _repo(tmp_path)
    monkeypatch.setattr(sheets, "REPO_ROOT", root)

    assert sheets.main(["--check"]) == 1
    err = capsys.readouterr().err
    assert "ERROR: missing API spec sheet: diagrams/api_specs/README.md" in err

    assert sheets.main([]) == 0
    out = capsys.readouterr().out
    assert "diagrams/api_specs/widget-service.md: written" in out

    assert sheets.main([]) == 0
    assert "API spec sheets unchanged" in capsys.readouterr().out

    assert sheets.main(["--check"]) == 0
    assert "API spec sheets are current" in capsys.readouterr().out


def test_main_builds_the_swagger_tree_and_reports_misconfiguration(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _repo(tmp_path)
    monkeypatch.setattr(sheets, "REPO_ROOT", root)
    assets = _assets(tmp_path)

    code = sheets.main(["--swagger-ui-dir", str(tmp_path / "out"), "--swagger-assets", str(assets)])
    assert code == 0
    assert "Swagger UI consoles: 7 files" in capsys.readouterr().out
    assert (tmp_path / "out" / "widget-service" / "index.html").is_file()

    # The default assets directory is node_modules/swagger-ui-dist under the root; absent here.
    assert sheets.main(["--swagger-ui-dir", str(tmp_path / "out2")]) == 2
    err = capsys.readouterr().err
    assert "ERROR: swagger-ui-dist assets missing from" in err and "npm ci" in err


def test_generator_runs_as_a_script_without_site_packages(tmp_path: Path) -> None:
    """``python -S diagrams/generate.py --check`` must keep working: the render path is stdlib-only."""
    import subprocess

    completed = subprocess.run(
        [sys.executable, "-S", "diagrams/api_specs/generate.py", "--check"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    assert "API spec sheets are current" in completed.stdout


# ─── the committed catalogue ─────────────────────────────────────────────────


def _generate_openapi() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "gco_generate_openapi", REPO_ROOT / "scripts" / "generate_openapi.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_committed_sheets_are_current() -> None:
    """The doc gate: a route or model change must regenerate the sheets in the same PR."""
    assert sheets.api_contract_issues(REPO_ROOT) == []


def test_committed_catalogue_covers_every_exported_service() -> None:
    exported = set(_generate_openapi().SERVICE_NAMES)
    assert set(sheets.service_names(REPO_ROOT / "docs" / "openapi")) == exported
    for service in exported:
        sheet = REPO_ROOT / "diagrams" / "api_specs" / f"{service}.md"
        assert sheets.banner(service) in sheet.read_text(encoding="utf-8")


def test_committed_sheets_link_only_to_things_that_exist() -> None:
    """Sibling links resolve in the catalogue; explicit anchors back every fragment link."""
    catalogue = REPO_ROOT / "diagrams" / "api_specs"
    for path in catalogue.glob("*.md"):
        text = path.read_text(encoding="utf-8")
        anchors = set(re.findall(r'<a id="([^"]+)"></a>', text))
        for target in re.findall(r"\]\(([^)\s]+)\)", text):
            if target.startswith("#"):
                assert target[1:] in anchors, f"{path.name}: dangling fragment {target}"
            elif not target.startswith(("http://", "https://")):
                assert (catalogue / target).is_file(), f"{path.name}: dangling link {target}"


def test_committed_sheets_use_the_repository_urls_from_mkdocs() -> None:
    site_url, repo_url = sheets.read_site_config(REPO_ROOT / "mkdocs.yml")
    readme = (REPO_ROOT / "diagrams" / "api_specs" / "README.md").read_text(encoding="utf-8")
    assert f"{site_url}{sheets.SWAGGER_SITE_PREFIX}/manifest-processor/" in readme
    assert f"{repo_url}/blob/main/docs/openapi/manifest-processor.json" in readme
